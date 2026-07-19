"""Internal-communication "Information" module.

An Information is a one-way institutional broadcast (never a conversation): an
administrator writes a titled message at a priority level, targets an audience with
the shared destination referential (``cible_activite``), and each recipient's
delivery is tracked (envoye -> lu -> confirme). Members read them in a permanent
feed. Recipients are DEDUPLICATED across overlapping target segments by the unique
(information, membre) constraint, so a member never receives the same Information
twice. Reading is only recorded on an explicit open; a confirmation only on an
explicit "J'ai pris connaissance".
"""
# ruff: noqa: E501
from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import cibles_activite as ca
from . import db
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["informations"])

_PRIORITES = "^(normale|importante|urgente)$"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# --- Payloads ---------------------------------------------------------------

class Croisement(BaseModel):
    """Fine-grained crossed targeting: a commission INSIDE a coordination or a
    stewardship, optionally narrowed to a sub-group. Every provided criterion is
    ANDed, so "Chorale de la coordination Afrique" reaches exactly that audience."""
    commission_id: str | None = None
    intendance_id: str | None = None
    coordination_id: str | None = None
    groupe: str | None = Field(default=None, max_length=160)


class CibleRef(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    cible_id: str | None = None
    # Manual selection: explicit member ids, used with the reserved code "selection".
    membre_ids: list[str] | None = Field(default=None, max_length=2000)
    # Crossed targeting, used with the reserved code "croisement".
    croisement: Croisement | None = None
    # Display label (unit name) persisted so the editor's chips stay readable.
    libelle: str | None = Field(default=None, max_length=160)


class InformationIn(BaseModel):
    titre: str = Field(min_length=1, max_length=200)
    sous_titre: str | None = Field(default=None, max_length=200)
    contenu: str = Field(default="", max_length=20000)
    priorite: str = Field(default="normale", pattern=_PRIORITES)
    auteur: str | None = Field(default=None, max_length=160)
    signature: str | None = Field(default=None, max_length=200)
    signature_url: str | None = Field(default=None, max_length=500)
    protege: bool = False
    institutionnelle: bool = False
    canaux: list[str] | None = Field(default=None, max_length=6)
    requiert_accuse: bool = False
    lecture_vocale_auto: bool = True
    lien_url: str | None = Field(default=None, max_length=1000)
    action_label: str | None = Field(default=None, max_length=80)
    action_url: str | None = Field(default=None, max_length=1000)
    publier_le: str | None = None
    expire_le: str | None = None
    epingle_jusqu: str | None = None
    cibles: list[CibleRef] = Field(default_factory=list)


class InformationPatch(BaseModel):
    titre: str | None = Field(default=None, max_length=200)
    sous_titre: str | None = Field(default=None, max_length=200)
    contenu: str | None = Field(default=None, max_length=20000)
    priorite: str | None = Field(default=None, pattern=_PRIORITES)
    auteur: str | None = Field(default=None, max_length=160)
    signature: str | None = Field(default=None, max_length=200)
    signature_url: str | None = Field(default=None, max_length=500)
    protege: bool | None = None
    institutionnelle: bool | None = None
    canaux: list[str] | None = Field(default=None, max_length=6)
    requiert_accuse: bool | None = None
    lecture_vocale_auto: bool | None = None
    lien_url: str | None = Field(default=None, max_length=1000)
    action_label: str | None = Field(default=None, max_length=80)
    action_url: str | None = Field(default=None, max_length=1000)
    publier_le: str | None = None
    expire_le: str | None = None
    epingle_jusqu: str | None = None
    cibles: list[CibleRef] | None = None


# --- Serialisation ----------------------------------------------------------

def _info_dict(r: dict[str, Any]) -> dict[str, Any]:
    cibles = r.get("cibles")
    if isinstance(cibles, str):
        cibles = json.loads(cibles or "[]")
    return {
        "id": str(r["id"]), "titre": r.get("titre"), "sous_titre": r.get("sous_titre"),
        "contenu": r.get("contenu"), "priorite": r.get("priorite"), "auteur": r.get("auteur"),
        "statut": r.get("statut"), "requiert_accuse": bool(r.get("requiert_accuse")),
        "signature": r.get("signature"), "signature_url": r.get("signature_url"),
        "protege": bool(r.get("protege")), "institutionnelle": bool(r.get("institutionnelle")),
        "canaux": (json.loads(r["canaux"]) if isinstance(r.get("canaux"), str) else r.get("canaux")) or ["application", "telegram"],
        "lecture_vocale_auto": bool(r.get("lecture_vocale_auto")),
        "lien_url": r.get("lien_url"), "action_label": r.get("action_label"), "action_url": r.get("action_url"),
        "audio_url": r.get("audio_url"), "image_url": r.get("image_url"), "document_url": r.get("document_url"),
        "publier_le": r.get("publier_le"), "expire_le": r.get("expire_le"), "epingle_jusqu": r.get("epingle_jusqu"),
        "cibles": cibles or [], "cree_le": r.get("cree_le"), "envoye_le": r.get("envoye_le"),
    }


def _colonnes(payload: InformationIn | InformationPatch) -> dict[str, Any]:
    champs = payload.model_dump(exclude_unset=True)
    out: dict[str, Any] = {}
    for k in ("titre", "sous_titre", "contenu", "priorite", "auteur", "signature", "signature_url",
              "protege", "institutionnelle", "requiert_accuse",
              "lecture_vocale_auto", "lien_url", "action_label", "action_url",
              "publier_le", "expire_le", "epingle_jusqu"):
        if k not in champs:
            continue
        # titre/contenu are NOT NULL: a patch that explicitly clears them is ignored
        # rather than crashing the UPDATE with a constraint violation.
        if k in ("titre", "contenu") and (champs[k] is None or str(champs[k]).strip() == ""):
            continue
        out[k] = champs[k]
    if "cibles" in champs and champs["cibles"] is not None:
        out["cibles"] = json.dumps([c if isinstance(c, dict) else c.model_dump() for c in payload.cibles or []])
    if "canaux" in champs and champs["canaux"] is not None:
        # In-app is always included; the rest are the admin's channel choices.
        valides = {"application", "push", "telegram", "email", "sms"}
        choisis = [c for c in (payload.canaux or []) if c in valides]
        if "application" not in choisis:
            choisis.insert(0, "application")
        out["canaux"] = json.dumps(choisis)
    return out


def _info_ou_404(info_id: str, role: str | None) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM information WHERE id = %s", (info_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="information inconnue")
    return row


# --- Recipient resolution (deduplicated) ------------------------------------

def _resoudre_destinataires(cibles: list[dict[str, Any]], role: str | None) -> list[str]:
    """Distinct active member ids for the union of the chosen destination segments.

    Each segment reuses the shared predicate builder (``cibles_activite``); the union
    plus the DISTINCT deduplicates a member reachable through several segments."""
    ids: set[str] = set()
    for c in cibles:
        code = str(c.get("code") or "").strip()
        if not code:
            continue
        # Crossed targeting: every provided criterion is ANDed over the members.
        if code == "croisement":
            cr = c.get("croisement") or {}
            preds: list[str] = ["m.statut = 'actif'"]
            params: list[Any] = []
            if cr.get("commission_id") and _UUID_RE.match(str(cr["commission_id"])):
                preds.append("m.commission_id = %s")
                params.append(str(cr["commission_id"]))
            if cr.get("intendance_id") and _UUID_RE.match(str(cr["intendance_id"])):
                preds.append("m.intendance_id = %s")
                params.append(str(cr["intendance_id"]))
            if cr.get("coordination_id") and _UUID_RE.match(str(cr["coordination_id"])):
                # A member belongs to a coordination THROUGH their stewardship.
                preds.append("EXISTS (SELECT 1 FROM intendance i WHERE i.id = m.intendance_id AND i.coordination_id = %s)")
                params.append(str(cr["coordination_id"]))
            if cr.get("groupe"):
                preds.append("m.groupe = %s")
                params.append(str(cr["groupe"]))
            if len(preds) > 1:  # at least one real criterion beyond the active guard
                rows = db.fetch_all(f"SELECT DISTINCT m.id FROM membre m WHERE {' AND '.join(preds)}", tuple(params), role=role)
                ids.update(str(r["id"]) for r in rows)
            continue
        # Manual selection: explicit member ids chosen one by one in the editor.
        # Only ACTIVE members among them are retained, like every other segment.
        if code == "selection":
            bruts = [str(m) for m in (c.get("membre_ids") or []) if _UUID_RE.match(str(m))]
            if bruts:
                rows = db.fetch_all(
                    "SELECT id FROM membre WHERE statut = 'actif' AND id = ANY(%s)",
                    (bruts,), role=role,
                )
                ids.update(str(r["id"]) for r in rows)
            continue
        cible = ca.cible_valide_pour_creation(code, role)
        if not cible:
            continue
        # A unit target carries a uuid; reject a malformed one here (a clean skip)
        # rather than letting Postgres raise an opaque 500 on the uuid cast.
        cible_id = c.get("cible_id")
        if cible.get("type_regle") == "unite" and (not cible_id or not _UUID_RE.match(str(cible_id))):
            continue
        predicat, params = ca.predicat_membres(cible, cible_id)
        rows = db.fetch_all(f"SELECT DISTINCT m.id FROM membre m WHERE {predicat}", tuple(params), role=role)
        ids.update(str(r["id"]) for r in rows)
    return sorted(ids)


# --- Admin: CRUD ------------------------------------------------------------

@router.get("/admin/informations")
def admin_liste(statut: str | None = None, user: Annotated[UserMe, Depends(require_permission("informations.consulter"))] = ...) -> list[dict[str, Any]]:
    if statut:
        rows = db.fetch_all("SELECT * FROM information WHERE statut = %s ORDER BY cree_le DESC", (statut,), role=user.role)
    else:
        rows = db.fetch_all("SELECT * FROM information ORDER BY cree_le DESC", (), role=user.role)
    return [_info_dict(r) for r in rows]


@router.post("/admin/informations", status_code=status.HTTP_201_CREATED)
def admin_creer(payload: InformationIn, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    cols = _colonnes(payload)
    cols.setdefault("cibles", json.dumps([c.model_dump() for c in payload.cibles]))
    noms = list(cols.keys()) + ["cree_par"]
    ph = ", ".join(["%s"] * len(noms))
    vals = [*cols.values(), user.id]
    row = db.execute(f"INSERT INTO information ({', '.join(noms)}) VALUES ({ph}) RETURNING *", tuple(vals), role=user.role)
    return _info_dict(row or {})


@router.get("/admin/informations/{info_id}")
def admin_detail(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.consulter"))]) -> dict[str, Any]:
    return _info_dict(_info_ou_404(info_id, user.role))


@router.patch("/admin/informations/{info_id}")
def admin_modifier(info_id: str, payload: InformationPatch, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    info = _info_ou_404(info_id, user.role)
    # Only an unsent draft (or a scheduled one) may be edited: once sent, members
    # may already have read the exact wording, so it must not change under them.
    if info.get("statut") not in ("brouillon", "programme"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seul un brouillon peut être modifié: une information envoyée ou archivée est figée.")
    cols = _colonnes(payload)
    if not cols:
        return _info_dict(info)
    sets = ", ".join(f"{k} = %s" for k in cols) + ", maj_le = now()"
    row = db.execute(f"UPDATE information SET {sets} WHERE id = %s RETURNING *", (*cols.values(), info_id), role=user.role)
    return _info_dict(row or {})


# Media attached to an Information (voice note, cover image, document), sent by the
# editor as a data URL and stored inline, like the event attachments in inline mode.
# The cap bounds the row size (~4/3 base64 overhead: 3.5 MB of data URL is roughly a
# 2.5 MB file, several minutes of an Opus voice note).
_MAX_MEDIA_DATA_URL = 3_500_000
_MEDIA_TYPES: dict[str, frozenset[str]] = {
    "audio": frozenset({"audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-m4a", "audio/aac"}),
    "image": frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}),
    "document": frozenset({
        "application/pdf", "text/plain",
        "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }),
}
_MEDIA_COLONNE = {"audio": "audio_url", "image": "image_url", "document": "document_url"}


class MediaIn(BaseModel):
    kind: str = Field(pattern="^(audio|image|document)$")
    # Empty string clears the media; otherwise a data URL whose declared type must
    # belong to the kind's allow-list (active-content types are never accepted).
    data_url: str = Field(max_length=_MAX_MEDIA_DATA_URL)


@router.post("/admin/informations/{info_id}/media")
def admin_media(info_id: str, payload: MediaIn, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    """Attach (or clear) a voice note, cover image or document on a draft."""
    info = _info_ou_404(info_id, user.role)
    if info.get("statut") not in ("brouillon", "programme"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seul un brouillon peut recevoir un média.")
    colonne = _MEDIA_COLONNE[payload.kind]
    valeur: str | None = None
    if payload.data_url:
        # MediaRecorder emits parameterised types (audio/webm;codecs=opus): accept
        # optional parameters between the mime and ";base64", validate the bare mime.
        m = re.match(r"^data:([a-zA-Z0-9.+/-]+)(?:;[a-zA-Z0-9=+._-]+)*;base64,", payload.data_url)
        if not m:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="format attendu: data URL base64")
        if m.group(1).lower() not in _MEDIA_TYPES[payload.kind]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"type non autorisé pour {payload.kind}: {m.group(1)}")
        valeur = payload.data_url
    row = db.execute(f"UPDATE information SET {colonne} = %s, maj_le = now() WHERE id = %s RETURNING *", (valeur, info_id), role=user.role)
    return _info_dict(row or {})


@router.delete("/admin/informations/{info_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_supprimer(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> None:
    info = _info_ou_404(info_id, user.role)
    # A sent OR archived information carries per-member delivery history: deleting it
    # would CASCADE-wipe the read/confirm records. Only an unsent draft is deletable;
    # a sent one is archived, never destroyed.
    if info.get("statut") not in ("brouillon", "programme"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Une information diffusée ne se supprime pas (son suivi de lecture serait perdu): elle s'archive.")
    db.execute("DELETE FROM information WHERE id = %s", (info_id,), role=user.role)


@router.post("/admin/informations/{info_id}/apercu-destinataires")
def admin_apercu(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.consulter"))]) -> dict[str, Any]:
    info = _info_ou_404(info_id, user.role)
    cibles = info.get("cibles")
    if isinstance(cibles, str):
        cibles = json.loads(cibles or "[]")
    ids = _resoudre_destinataires(list(cibles or []), user.role)
    return {"destinataires_uniques": len(ids), "segments": len(cibles or [])}


@router.post("/admin/informations/{info_id}/publier")
def admin_publier(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    """Materialise the deduplicated recipient set, mark the Information as sent, and
    relay it professionally on the selected channels (Telegram)."""
    info = _info_ou_404(info_id, user.role)
    if info.get("statut") == "archive":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Information archivée.")
    # Mandatory display duration: an Information can never be published without an
    # explicit end of its priority window (no indefinitely active Information).
    if not info.get("expire_le"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Durée d'affichage obligatoire : définissez une date de fin d'activation avant de publier.")
    cibles = info.get("cibles")
    if isinstance(cibles, str):
        cibles = json.loads(cibles or "[]")
    ids = _resoudre_destinataires(list(cibles or []), user.role)
    if not ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucun destinataire: choisissez au moins un ciblage qui vise des membres.")
    # One multi-row insert, ON CONFLICT DO NOTHING so re-publishing never duplicates.
    tuple_sql = ", ".join(["(%s, %s)"] * len(ids))
    flat: list[Any] = []
    for mid in ids:
        flat.extend([info_id, mid])
    db.execute(
        f"INSERT INTO information_destinataire (information_id, membre_id) VALUES {tuple_sql} ON CONFLICT (information_id, membre_id) DO NOTHING",
        tuple(flat), role=user.role,
    )
    db.execute("UPDATE information SET statut = 'envoye', envoye_le = coalesce(envoye_le, now()), maj_le = now() WHERE id = %s", (info_id,), role=user.role)
    # Relay on the selected channels. The in-app feed is always the source of truth.
    canaux = info.get("canaux")
    if isinstance(canaux, str):
        canaux = json.loads(canaux or "[]")
    telegram = {"envoyes": 0, "eligibles": 0, "tronques": 0}
    if "telegram" in (canaux or ["application", "telegram"]):
        from . import information_diffusion
        telegram = information_diffusion.diffuser_telegram(info_id, info, ids, user.role)
    return {"ok": True, "destinataires": len(ids), "telegram": telegram}


@router.post("/admin/informations/{info_id}/archiver")
def admin_archiver(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    _info_ou_404(info_id, user.role)
    row = db.execute("UPDATE information SET statut = 'archive', maj_le = now() WHERE id = %s RETURNING *", (info_id,), role=user.role)
    return _info_dict(row or {})


@router.get("/admin/informations/{info_id}/statistiques")
def admin_stats(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.consulter"))]) -> dict[str, Any]:
    _info_ou_404(info_id, user.role)
    r = db.fetch_one(
        "SELECT count(*) AS total, count(*) FILTER (WHERE statut IN ('lu', 'confirme')) AS lus, "
        "count(*) FILTER (WHERE statut = 'confirme') AS confirmes "
        "FROM information_destinataire WHERE information_id = %s",
        (info_id,), role=user.role,
    ) or {}
    total = int(r.get("total") or 0)
    lus = int(r.get("lus") or 0)
    confirmes = int(r.get("confirmes") or 0)
    return {
        "destinataires": total, "lus": lus, "confirmes": confirmes, "non_lus": total - lus,
        "taux_lecture": round(lus / total * 100, 1) if total else 0.0,
        "taux_confirmation": round(confirmes / total * 100, 1) if total else 0.0,
    }


# --- Member: feed -----------------------------------------------------------

def _membre_ou_403(user: UserMe) -> str:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lié à un membre")
    return user.membre_id


_FEED_SELECT = (
    "SELECT i.id, i.titre, i.sous_titre, i.contenu, i.priorite, i.auteur, i.statut, i.requiert_accuse, "
    "i.lecture_vocale_auto, i.lien_url, i.action_label, i.action_url, i.audio_url, i.image_url, i.document_url, "
    "i.publier_le, i.expire_le, i.epingle_jusqu, i.cibles, i.cree_le, i.envoye_le, i.signature, i.signature_url, i.canaux, "
    "d.statut AS d_statut, d.lu_le, d.confirme_le "
    "FROM information_destinataire d JOIN information i ON i.id = d.information_id "
    "WHERE d.membre_id = %s AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now())"
)


@router.get("/membres/me/informations")
def feed_membre(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, Any]]:
    mid = _membre_ou_403(user)
    rows = db.fetch_all(
        _FEED_SELECT + " ORDER BY (i.priorite = 'urgente') DESC, "
        "(i.epingle_jusqu IS NOT NULL AND i.epingle_jusqu > now()) DESC, "
        "(i.priorite = 'importante') DESC, i.envoye_le DESC",
        (mid,), role=user.role,
    )
    out = []
    for r in rows:
        d = _info_dict(r)
        d.pop("cibles", None)  # the targeting is internal; a member never sees who else was targeted.
        d.update({"lu": r.get("d_statut") in ("lu", "confirme"), "confirme": r.get("d_statut") == "confirme",
                  "lu_le": r.get("lu_le"), "confirme_le": r.get("confirme_le")})
        out.append(d)
    return out


@router.get("/membres/me/informations/compteur")
def compteur_non_lus(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, int]:
    mid = _membre_ou_403(user)
    r = db.fetch_one(
        "SELECT count(*) AS n FROM information_destinataire d JOIN information i ON i.id = d.information_id "
        "WHERE d.membre_id = %s AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now()) "
        "AND d.statut NOT IN ('lu', 'confirme')",
        (mid,), role=user.role,
    ) or {}
    return {"non_lus": int(r.get("n") or 0)}


@router.get("/membres/me/informations/{info_id}")
def detail_membre(info_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    mid = _membre_ou_403(user)
    rows = db.fetch_all(_FEED_SELECT + " AND i.id = %s", (mid, info_id), role=user.role)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="information indisponible")
    # Opening the detail records the read (never a push or a list view). Never
    # downgrades an existing confirmation.
    db.execute(
        "UPDATE information_destinataire SET statut = 'lu', lu_le = coalesce(lu_le, now()) "
        "WHERE information_id = %s AND membre_id = %s AND statut NOT IN ('lu', 'confirme')",
        (info_id, mid), role=user.role,
    )
    r = rows[0]
    d = _info_dict(r)
    d.pop("cibles", None)
    d.update({"lu": True, "confirme": r.get("d_statut") == "confirme", "confirme_le": r.get("confirme_le")})
    return d


@router.post("/membres/me/informations/{info_id}/confirmer")
def confirmer_membre(info_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    mid = _membre_ou_403(user)
    # Confirm only an information the member actually receives, that is still sent and
    # not expired (same scope as the feed), so a confirmation can never be recorded on
    # a withdrawn or out-of-scope message.
    upd = db.execute(
        "UPDATE information_destinataire d SET statut = 'confirme', confirme_le = coalesce(d.confirme_le, now()), "
        "lu_le = coalesce(d.lu_le, now()) FROM information i "
        "WHERE d.information_id = i.id AND d.information_id = %s AND d.membre_id = %s "
        "AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now()) RETURNING d.confirme_le",
        (info_id, mid), role=user.role,
    )
    if not upd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="information indisponible")
    return {"ok": True, "confirme_le": upd.get("confirme_le")}
