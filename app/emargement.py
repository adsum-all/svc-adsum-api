"""External lightweight attendance check-in (public, no login).

A person confirms their presence at an event from a public page, identifying
with their country dial code, phone number and last name. The flow is:

1. ``GET /public/emargement/{evenement_id}`` returns the event card, but only
   when the organizer enabled ``emargement_externe`` on that event.
2. ``POST /public/emargement/{evenement_id}/identifier`` matches the person to a
   real member (dial code + phone + last name) and returns a minimal confirmation
   card plus a short-lived signed token bound to that member and that event.
3. ``POST /public/emargement/{evenement_id}/participation`` finalizes the
   attendance questionnaire using the signed token.

Security model (deny-by-default): the surface is closed unless the event opted in
AND the attendance window is open. The submit never trusts a member id sent by
the client: it only accepts the HMAC token issued at step 2, so a caller can
only mark the member they proved they know, and only for the bound event. The
write goes through :func:`app.participation.upsert_participation`, the single
source of truth, so a person is counted exactly once across the member space,
the QR scan and this external link (unique per event and member).
"""
# ruff: noqa: E501
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import audit, db, ratelimit, temps
from .config import settings
from .participation import FENETRE_FIN_SQL, upsert_participation

router = APIRouter(prefix="/api/v1/public", tags=["emargement"])

_STATUTS = ("present", "partiel", "absent")
_MODALITES = ("presentiel", "en_ligne")
_TOKEN_TTL_SECONDS = 900  # 15 minutes to answer the questionnaire after identifying


# --- Signed token binding a member to an event ------------------------------

def _sign(membre_id: str, evenement_id: str, ttl: int = _TOKEN_TTL_SECONDS) -> str:
    """Issue an opaque HMAC token proving identification of ``membre_id`` for ``evenement_id``.

    The token is ``base64url(membre_id:evenement_id:exp:hmac)``; it carries no
    secret and is verified server-side. UUIDs contain no ``:`` so the fields are
    unambiguous.
    """
    exp = int(time.time()) + ttl
    payload = f"{membre_id}:{evenement_id}:{exp}"
    sig = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def _verify(token: str, evenement_id: str) -> str | None:
    """Return the member id a valid token was issued for, or ``None``.

    Rejects a bad signature, an expired token, or a token bound to another event.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        membre_id, ev_id, exp_s, sig = raw.rsplit(":", 3)
    except ValueError:
        # Covers a bad base64 (binascii.Error), a non-utf8 payload
        # (UnicodeDecodeError) and a token with too few fields: all ValueError.
        return None
    payload = f"{membre_id}:{ev_id}:{exp_s}"
    expected = hmac.new(settings.jwt_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if ev_id != evenement_id:
        return None
    try:
        if int(exp_s) < int(time.time()):
            return None
    except ValueError:
        return None
    return membre_id


# --- Phone matching ----------------------------------------------------------

def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _norm_matricule(value: str) -> str:
    """Canonical form of a member matricule for comparison: upper case, no spaces.

    Members carry a human-readable, assigned matricule (e.g. ``ADS-000001`` or
    ``ADS-2026-000001``). It is matched verbatim once trimmed and upper-cased, so
    both stored formats work without inventing a second identifier.
    """
    return re.sub(r"\s+", "", value or "").upper()


def _phone_matches(in_indicatif: str, in_numero: str, stored_indicatif: str | None, stored_numero: str | None) -> bool:
    """Whether an entered (dial code, number) identifies a stored member phone.

    Robust to format variance: the stored number may or may not already embed the
    dial code, and either side may carry a national trunk zero or spaces. The last
    name is matched separately, so this only has to disambiguate the phone.
    """
    ii = _digits(in_indicatif).lstrip("0")
    nn = _digits(in_numero).lstrip("0")
    si = _digits(stored_indicatif).lstrip("0")
    sn = _digits(stored_numero).lstrip("0")
    if not nn or not sn:
        return False
    full_in = ii + nn
    return full_in == (si + sn) or full_in == sn or nn == sn


# --- Event helpers -----------------------------------------------------------

def _event_or_404(evenement_id: str) -> dict[str, object]:
    row = db.fetch_one(
        f"SELECT e.id, e.titre, e.lieu, e.debut, e.fin, e.fuseau_horaire, e.emargement_externe, "
        f"coalesce(e.type_diffusion, 'aucun') AS type_diffusion, "
        f"(e.debut IS NOT NULL AND now() >= e.debut) AS demarree, "
        f"(e.debut IS NOT NULL AND now() > {FENETRE_FIN_SQL}) AS cloture, "
        f"{FENETRE_FIN_SQL} AS cloture_le "
        "FROM evenement e WHERE e.id = %s",
        (evenement_id,),
    )
    if not row or not row["emargement_externe"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="émargement externe indisponible pour cette activité")
    return row


def _guard_window(ev: dict[str, object]) -> None:
    if not ev["demarree"]:
        quand = temps.formater_instant(ev["debut"], ev.get("fuseau_horaire")) if ev["debut"] else ""
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"l'émargement ouvrira au début de l'activité ({quand})")
    if ev["cloture"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="l'émargement de cette activité est clôturé")


# --- Schemas -----------------------------------------------------------------

class EventCard(BaseModel):
    id: str
    titre: str
    lieu: str | None = None
    debut: str | None = None
    fin: str | None = None
    en_ligne: bool = False
    ouvert: bool
    cloture: bool
    cloture_le: str | None = None


class IdentifierIn(BaseModel):
    """Identify a member either by matricule (priority) or by dial code + phone + last name.

    Only one mode is used per request: the matricule is tried first when provided,
    otherwise the phone triple. The two modes are never required together.
    """

    matricule: str | None = Field(default=None, max_length=32)
    code_membre: str | None = Field(default=None, max_length=32)
    indicatif: str | None = Field(default=None, max_length=8)
    telephone: str | None = Field(default=None, max_length=32)
    nom: str | None = Field(default=None, max_length=120)


class IdentiteOut(BaseModel):
    token: str
    prenom: str | None = None
    nom: str
    matricule: str
    deja_enregistre: bool = False
    statut: str | None = None


class EmargementIn(BaseModel):
    token: str
    statut: str
    modalite: str | None = None
    avis: str | None = Field(default=None, max_length=2000)
    note: int | None = None


# --- Endpoints ---------------------------------------------------------------

@router.get("/emargement/{evenement_id}", response_model=EventCard)
def carte_evenement(evenement_id: str) -> EventCard:
    """Public event card for the external check-in page (opt-in events only)."""
    ev = _event_or_404(evenement_id)
    return EventCard(
        id=str(ev["id"]),
        titre=str(ev["titre"]),
        lieu=ev["lieu"],
        debut=ev["debut"].isoformat() if ev["debut"] else None,
        fin=ev["fin"].isoformat() if ev.get("fin") else None,
        en_ligne=str(ev.get("type_diffusion") or "aucun") != "aucun",
        ouvert=bool(ev["demarree"]) and not bool(ev["cloture"]),
        cloture=bool(ev["cloture"]),
        cloture_le=ev["cloture_le"].isoformat() if ev.get("cloture_le") else None,
    )


_ECHEC_MATRICULE = "Matricule introuvable. Vérifiez votre matricule, ou identifiez-vous avec votre numéro et votre nom."
_ECHEC_TELEPHONE = "Identification impossible. Vérifiez votre indicatif, votre numéro et votre nom de famille."


def _identifier_par_matricule(matricule: str) -> dict[str, object]:
    """Resolve a member from their matricule (priority mode). Uniform failure.

    Also matches the historical matricule so a member holding an old card keeps
    checking in during the transition to the canonical format.
    """
    norm = _norm_matricule(matricule)
    rows = db.fetch_all(
        "SELECT id, prenoms, nom, matricule FROM membre "
        "WHERE upper(replace(matricule, ' ', '')) = %s OR upper(replace(coalesce(matricule_historique, ''), ' ', '')) = %s",
        (norm, norm),
    )
    if len(rows) != 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_ECHEC_MATRICULE)
    return rows[0]


def _identifier_par_code_membre(code: str) -> dict[str, object]:
    """Resolve a member from their external member code (uppercase, unique). Uniform failure."""
    rows = db.fetch_all(
        "SELECT id, prenoms, nom, matricule FROM membre WHERE upper(code_membre) = %s AND code_membre IS NOT NULL",
        (code.strip().upper(),),
    )
    if len(rows) != 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_ECHEC_MATRICULE)
    return rows[0]


def _identifier_par_telephone(indicatif: str, telephone: str, nom: str) -> dict[str, object]:
    """Resolve a member from dial code + phone + last name (fallback mode).

    Two factors required (phone AND name) to resist impersonation. Uniform failure.
    """
    candidates = db.fetch_all(
        "SELECT id, prenoms, nom, matricule, telephone, indicatif_telephone FROM membre "
        "WHERE upper(nom) = %s AND telephone IS NOT NULL",
        (nom.strip().upper(),),
    )
    matches = [
        c for c in candidates
        if _phone_matches(indicatif, telephone, c.get("indicatif_telephone"), c.get("telephone"))
    ]
    if len(matches) != 1:
        # 0 (not found) or >1 (ambiguous) both yield the same neutral answer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_ECHEC_TELEPHONE)
    return matches[0]


@router.post("/emargement/{evenement_id}/identifier", response_model=IdentiteOut)
def identifier(evenement_id: str, payload: IdentifierIn, request: Request) -> IdentiteOut:
    """Identify a member, matricule first then phone + last name, and issue a bound token.

    Matricule is the priority mode (a member's own assigned, communicated code);
    when it is not provided, the dial code + phone + last name triple is used as a
    fallback. The two modes are never required together. On failure the message is
    uniform so the endpoint cannot confirm whether a matricule, phone or name exists.
    A per-IP anti-enumeration guard throttles repeated FAILURES only (so a live event
    behind one venue IP is never blocked), which bounds harvesting names/matricules by
    iterating the sequential matricule space.
    """
    ev = _event_or_404(evenement_id)
    _guard_window(ev)
    ratelimit.emargement_guard(request)
    try:
        if payload.matricule and payload.matricule.strip():
            m = _identifier_par_matricule(payload.matricule)
        elif payload.code_membre and payload.code_membre.strip():
            m = _identifier_par_code_membre(payload.code_membre)
        elif payload.indicatif and payload.telephone and payload.nom:
            m = _identifier_par_telephone(payload.indicatif, payload.telephone, payload.nom)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Fournissez votre matricule, votre code membre, ou votre indicatif, votre numéro et votre nom.",
            )
    except HTTPException as exc:
        # A failed identification (not found / ambiguous) feeds the anti-enumeration
        # counter; a malformed request (400) does not.
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            ratelimit.emargement_failure(request)
        raise
    membre_id = str(m["id"])
    existing = db.fetch_one(
        "SELECT statut, valide FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
    )
    deja = bool(existing and existing["valide"])
    return IdentiteOut(
        token=_sign(membre_id, evenement_id),
        prenom=m.get("prenoms"),
        nom=str(m["nom"]),
        matricule=str(m["matricule"]),
        deja_enregistre=deja,
        statut=existing["statut"] if existing else None,
    )


def _resoudre_statut_modalite(existing: dict[str, object] | None, payload: EmargementIn) -> tuple[str, str | None]:
    """Resolve the final status and modality, honouring an existing scan.

    A scanned member is present on site; status and modality are fixed by the scan
    and the submitted values are ignored. Otherwise the declaration drives them,
    with the modality required unless the person declares an absence.
    """
    if existing and existing.get("source") == "scan":
        return "present", "presentiel"
    if payload.statut == "absent":
        return payload.statut, None
    if payload.modalite not in _MODALITES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="précisez présentiel ou en ligne")
    return payload.statut, payload.modalite


@router.post("/emargement/{evenement_id}/participation")
def emarger(evenement_id: str, payload: EmargementIn, request: Request) -> dict[str, object]:
    """Finalize the attendance questionnaire for the identified member.

    The member id comes only from the signed token, never from the client body.
    The write is a one-shot finalization that converges on the single
    ``participation`` source of truth, so the person is counted exactly once
    whatever the channel (member space, QR scan or this link).
    """
    membre_id = _verify(payload.token, evenement_id)
    if not membre_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session d'émargement expirée, recommencez l'identification")
    ev = _event_or_404(evenement_id)
    _guard_window(ev)
    if payload.statut not in _STATUTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut invalide")
    if payload.note is not None and not 1 <= payload.note <= 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="la note doit être comprise entre 1 et 5")

    existing = db.fetch_one(
        "SELECT statut, source, valide FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
    )
    # Already finalized (validated declaration or a completed scan+feedback):
    # immutable, counted once. The page shows the confirmation instead of resubmitting.
    if existing and existing["valide"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Votre présence est déjà enregistrée pour cette activité.")

    statut, modalite = _resoudre_statut_modalite(existing, payload)
    upsert_participation(
        evenement_id,
        membre_id,
        statut=statut,
        source="declaration",
        valide=True,
        modalite=modalite,
        role=None,
    )
    # Presence (above) is identifiable; the rating/comment go to the ANONYMOUS
    # evaluation table with no member link. The audit trail below records the
    # presence only, never the evaluation content, so nothing there can attribute it.
    from .participation import soumettre_evaluation

    evalue = soumettre_evaluation(evenement_id, membre_id, note=payload.note, avis=payload.avis, role=None)
    audit.log(
        None,
        "emargement_externe",
        "emargement_externe",
        "participation",
        evenement_id,
        {"membre_id": membre_id, "statut": statut, "modalite": modalite, "ip": request.client.host if request.client else None},
    )
    return {"ok": True, "statut": statut, "valide": True, "modalite": modalite, "evaluation_enregistree": evalue}
