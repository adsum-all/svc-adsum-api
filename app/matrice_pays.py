"""Country e-signature matrix and hand-signed attestation follow-up.

The admin-editable matrix says, per country, whether a hand-signed attestation is
required in addition to the electronic signature. When a member from such a
country is approved, a one-month attestation task opens: the member downloads a
one-page attestation (summarizing the electronically-approved documents), signs
it by hand, scans it and uploads it; the admin validates it. Deadlines, reminders
and automatic invalidation are handled by the daily job (see notifications.py).
"""
# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db, identifiants
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["attestation"])


DELAI_JOURS = 30  # deadline to return the signed attestation, from the approval date


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


def manuel_requis_pour(pays: str | None, role: str | None) -> bool:
    """Whether a hand-signed attestation is required for that country.

    Matches on the country name (FR or EN) or ISO code. An unlisted country
    defaults to requiring the manual attestation (conservative posture).
    """
    if not pays:
        return True
    row = db.fetch_one(
        "SELECT manuel_requis FROM pays_signature WHERE lower(nom) = lower(%s) OR lower(nom_en) = lower(%s) OR code_pays = upper(%s)",
        (pays, pays, pays),
        role=role,
    )
    if row is None:
        return True
    return bool(row["manuel_requis"])


def ouvrir_attestation_si_besoin(membre_id: str, role: str | None) -> bool:
    """On approval, open a manual-attestation task when the member's country
    requires it. Returns True when a task was opened."""
    m = db.fetch_one("SELECT pays, prenoms FROM membre WHERE id = %s", (membre_id,), role=role)
    if not m or not manuel_requis_pour(m.get("pays"), role):
        db.execute("UPDATE membre SET attestation_statut = 'not_required' WHERE id = %s", (membre_id,), role=role)
        return False
    existing = db.fetch_one(
        "SELECT id FROM attestation_manuelle WHERE membre_id = %s AND statut NOT IN ('accepted', 'invalidated')",
        (membre_id,),
        role=role,
    )
    if existing:
        return True
    att = db.execute(
        "INSERT INTO attestation_manuelle (membre_id, statut, echeance) VALUES (%s, 'awaiting', now() + (%s || ' days')::interval) RETURNING id",
        (membre_id, str(DELAI_JOURS)),
        role=role,
    )
    db.execute("UPDATE membre SET attestation_statut = 'awaiting' WHERE id = %s", (membre_id,), role=role)
    # The attestation to return is a tracked request: it shows in Mes demandes
    # (member) and in the member file (back office) with its own timeline.
    ticket = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, categorie, sous_categorie, statut, reference) "
        "VALUES (%s, 'document', 'Attestation signée à retourner', 'documents', 'attestation_retour', 'attente_membre', %s) RETURNING id",
        (membre_id, identifiants.next_reference(role)),
        role=role,
    )
    if att and ticket:
        from .demandes import _system_message

        db.execute(
            "UPDATE attestation_manuelle SET demande_id = %s WHERE id = %s",
            (str(ticket["id"]), str(att["id"])),
            role=role,
        )
        _system_message(
            str(ticket["id"]), role,
            "Votre pays exige une attestation signée à la main. Imprimez le document depuis Ma carte, signez-le, puis renvoyez la version scannée.",
        )
    from .notifications import notifier

    prenom = (str(m.get("prenoms") or "").split(" ")[0]) or "cher membre"
    ech = db.fetch_one("SELECT to_char(now() + (%s || ' days')::interval, 'DD/MM/YYYY') AS d", (str(DELAI_JOURS),), role=role)
    notifier(membre_id, role, "attestation_requise", {"prenom": prenom, "echeance": ech["d"] if ech else ""})
    return True


# --- Admin: country matrix --------------------------------------------------

class PaysPatch(BaseModel):
    manuel_requis: bool | None = None
    esignature_reconnue: bool | None = None
    note: str | None = None
    source: str | None = None


@router.get("/admin/pays-signature")
def list_pays(user: Annotated[UserMe, Depends(require_permission("matrice-pays.consulter"))]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT code_pays, nom, nom_en, esignature_reconnue, manuel_requis, source, note FROM pays_signature ORDER BY nom",
        (),
        role=user.role,
    )
    return [dict(r) for r in rows]


@router.patch("/admin/pays-signature/{code}")
def maj_pays(code: str, payload: PaysPatch, user: Annotated[UserMe, Depends(require_permission("matrice-pays.gerer"))]) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE pays_signature SET {sets}, maj_par = %s, maj_le = now() WHERE code_pays = %s RETURNING code_pays",
        (*fields.values(), user.id, code.upper()),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown country")
    audit.log(user.id, user.role, "maj_pays_signature", "pays_signature", code.upper(), fields)
    return {"ok": True}


# --- Member: attestation ----------------------------------------------------

def _texte_attestation(membre_id: str, role: str | None, langue: str) -> str:
    m = db.fetch_one("SELECT prenoms, nom FROM membre WHERE id = %s", (membre_id,), role=role)
    nom = f"{(m or {}).get('prenoms') or ''} {(m or {}).get('nom') or ''}".strip()
    # Join the readable document title (per key + version) instead of showing the
    # technical key, and fall back to the key/latest title if a version differs.
    title_col = "titre_en" if langue == "en" else "titre"
    docs = db.fetch_all(
        f"SELECT DISTINCT e.type, e.version, "
        f"coalesce(dc.{title_col}, dc2.{title_col}, e.type) AS titre "
        "FROM engagement e "
        "LEFT JOIN document_consentement dc ON dc.cle = e.type AND dc.version::text = e.version "
        "LEFT JOIN document_consentement dc2 ON dc2.cle = e.type AND dc2.actif = true "
        "WHERE e.membre_id = %s ORDER BY e.type",
        (membre_id,),
        role=role,
    )
    liste = ", ".join(f"{d['titre']} (v{d['version']})" for d in docs) or "-"
    date_str = datetime.now(tz=UTC).strftime("%d/%m/%Y")
    if langue == "en":
        return (
            f"I, the undersigned {nom}, hereby attest that I have read, understood and approved the following "
            f"documents of the Sacerdoce Royal: {liste}. I confirm my commitment as a member.\n\n"
            f"Date: {date_str}\nHandwritten signature: ______________________"
        )
    return (
        f"Je soussigné(e) {nom}, atteste avoir lu, compris et approuvé les documents suivants du Sacerdoce Royal : "
        f"{liste}. Je confirme mon engagement en qualité de membre.\n\n"
        f"Fait le : {date_str}\nSignature manuscrite : ______________________"
    )


@router.get("/membres/me/attestation")
def mon_attestation(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    membre_id, role = ctx
    m = db.fetch_one("SELECT attestation_statut, langue FROM membre WHERE id = %s", (membre_id,), role=role)
    statut = (m or {}).get("attestation_statut") or "not_required"
    att = db.fetch_one(
        "SELECT statut, to_char(echeance, 'YYYY-MM-DD') AS echeance, "
        "to_char(soumise_le, 'DD/MM/YYYY') AS soumise_le, motif_rejet "
        "FROM attestation_manuelle WHERE membre_id = %s ORDER BY ouverte_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    return {
        "requise": statut not in ("not_required",),
        "statut": (att or {}).get("statut") or statut,
        "echeance": (att or {}).get("echeance"),
        "soumise_le": (att or {}).get("soumise_le"),
        "motif_rejet": (att or {}).get("motif_rejet"),
        "texte": _texte_attestation(membre_id, role, str((m or {}).get("langue") or "fr")),
    }


class AttestationUploadIn(BaseModel):
    document_id: str


@router.post("/membres/me/attestation/upload")
def deposer_attestation(payload: AttestationUploadIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Attach the uploaded scan of the hand-signed attestation for admin review."""
    membre_id, role = ctx
    # Ownership check (HDS/RGPD): the document MUST belong to this member, so nobody
    # can reference another member's identity document into their own attestation.
    if not db.fetch_one(
        "SELECT 1 AS ok FROM document WHERE id = %s AND membre_id = %s",
        (payload.document_id, membre_id),
        role=role,
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="document introuvable ou non rattaché à ce membre")
    row = db.execute(
        "UPDATE attestation_manuelle SET document_id = %s, statut = 'under_review', soumise_le = now(), motif_rejet = NULL "
        "WHERE membre_id = %s AND statut IN ('awaiting', 'reminded', 'overdue', 'rejected') RETURNING id, demande_id",
        (payload.document_id, membre_id),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no attestation to return")
    db.execute("UPDATE membre SET attestation_statut = 'under_review' WHERE id = %s", (membre_id,), role=role)
    # Feed the linked tracking ticket: the transmitted scan appears in the
    # timeline (with the piece) and the request moves to validation.
    if row.get("demande_id"):
        from .demandes import _system_message

        did = str(row["demande_id"])
        db.execute(
            "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) "
            "SELECT %s, 'membre', trim(coalesce(prenoms,'')||' '||coalesce(nom,'')), 'Attestation signée transmise.', %s FROM membre WHERE id = %s",
            (did, payload.document_id, membre_id),
            role=role,
        )
        # A new scan starts a new validation round, even on a closed ticket:
        # reopen it so the member and the staff see one coherent truth.
        db.execute(
            "UPDATE demande SET statut = 'en_validation', maj_le = now(), clos_le = NULL, motif_cloture = NULL WHERE id = %s",
            (did,),
            role=role,
        )
        _system_message(did, role, "Document signé transmis. En cours de vérification par l'administration.")
    return {"ok": True, "statut": "under_review"}


# --- Admin: attestation review ----------------------------------------------

@router.get("/admin/attestations")
def list_attestations(user: Annotated[UserMe, Depends(require_permission("attestations.gerer"))]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT a.id, a.statut, a.document_id, to_char(a.echeance, 'YYYY-MM-DD') AS echeance, "
        "trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre, m.pays, m.email "
        "FROM attestation_manuelle a JOIN membre m ON m.id = a.membre_id "
        "WHERE a.statut NOT IN ('accepted', 'invalidated') ORDER BY a.echeance ASC NULLS LAST",
        (),
        role=user.role,
    )
    return [dict(r) for r in rows]


class ValiderAttestationIn(BaseModel):
    decision: str  # accepted | rejected
    motif: str | None = None  # shown to the member when rejected


@router.post("/admin/attestations/{attestation_id}/valider")
def valider_attestation(attestation_id: str, payload: ValiderAttestationIn, user: Annotated[UserMe, Depends(require_permission("attestations.gerer"))]) -> dict[str, object]:
    if payload.decision not in ("accepted", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown decision")
    motif = (payload.motif or "").strip() or None
    row = db.execute(
        "UPDATE attestation_manuelle SET statut = %s, valide_le = now(), valide_par = %s, "
        "motif_rejet = CASE WHEN %s = 'rejected' THEN %s ELSE NULL END "
        "WHERE id = %s RETURNING membre_id, demande_id",
        (payload.decision, user.id, payload.decision, motif, attestation_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown attestation")
    db.execute("UPDATE membre SET attestation_statut = %s WHERE id = %s", (payload.decision, row["membre_id"]), role=user.role)
    # Close or bounce the linked tracking ticket, with the decision on record.
    if row.get("demande_id"):
        from .demandes import _notify_ticket, _system_message

        did = str(row["demande_id"])
        if payload.decision == "accepted":
            db.execute(
                "UPDATE demande SET statut = 'resolue', motif_cloture = 'Attestation validée', clos_le = now(), maj_le = now() WHERE id = %s",
                (did,),
                role=user.role,
            )
            _system_message(did, user.role, "Attestation validée par l'administration. Demande résolue.")
            _notify_ticket(did, user.role, "Attestation validée",
                           "Votre attestation signée a été vérifiée et validée par l'administration. Merci.")
        else:
            db.execute(
                "UPDATE demande SET statut = 'attente_membre', maj_le = now() WHERE id = %s",
                (did,),
                role=user.role,
            )
            _system_message(did, user.role,
                            "Attestation refusée." + (f" Motif : {motif}." if motif else "") + " Merci de renvoyer le document signé.")
            _notify_ticket(did, user.role, "Attestation à renvoyer",
                           ("Votre attestation signée n'a pas pu être validée."
                            + (f" Motif : {motif}." if motif else "")
                            + " Ouvrez Ma carte pour renvoyer une version corrigée."))
    audit.log(user.id, user.role, "validation_attestation", "attestation_manuelle", attestation_id,
              {"decision": payload.decision, "motif": motif})
    return {"ok": True, "statut": payload.decision}
