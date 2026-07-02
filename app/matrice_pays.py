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

from . import audit, db
from .auth import current_user
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["attestation"])

require_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")

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
    db.execute(
        "INSERT INTO attestation_manuelle (membre_id, statut, echeance) VALUES (%s, 'awaiting', now() + (%s || ' days')::interval)",
        (membre_id, str(DELAI_JOURS)),
        role=role,
    )
    db.execute("UPDATE membre SET attestation_statut = 'awaiting' WHERE id = %s", (membre_id,), role=role)
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
def list_pays(user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT code_pays, nom, nom_en, esignature_reconnue, manuel_requis, source, note FROM pays_signature ORDER BY nom",
        (),
        role=user.role,
    )
    return [dict(r) for r in rows]


@router.patch("/admin/pays-signature/{code}")
def maj_pays(code: str, payload: PaysPatch, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
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
        "SELECT statut, to_char(echeance, 'YYYY-MM-DD') AS echeance FROM attestation_manuelle "
        "WHERE membre_id = %s ORDER BY ouverte_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    return {
        "requise": statut not in ("not_required",),
        "statut": (att or {}).get("statut") or statut,
        "echeance": (att or {}).get("echeance"),
        "texte": _texte_attestation(membre_id, role, str((m or {}).get("langue") or "fr")),
    }


class AttestationUploadIn(BaseModel):
    document_id: str


@router.post("/membres/me/attestation/upload")
def deposer_attestation(payload: AttestationUploadIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Attach the uploaded scan of the hand-signed attestation for admin review."""
    membre_id, role = ctx
    row = db.execute(
        "UPDATE attestation_manuelle SET document_id = %s, statut = 'under_review' "
        "WHERE membre_id = %s AND statut IN ('awaiting', 'reminded', 'overdue', 'rejected') RETURNING id",
        (payload.document_id, membre_id),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no attestation to return")
    db.execute("UPDATE membre SET attestation_statut = 'under_review' WHERE id = %s", (membre_id,), role=role)
    return {"ok": True, "statut": "under_review"}


# --- Admin: attestation review ----------------------------------------------

@router.get("/admin/attestations")
def list_attestations(user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
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


@router.post("/admin/attestations/{attestation_id}/valider")
def valider_attestation(attestation_id: str, payload: ValiderAttestationIn, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    if payload.decision not in ("accepted", "rejected"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown decision")
    row = db.execute(
        "UPDATE attestation_manuelle SET statut = %s, valide_le = now(), valide_par = %s WHERE id = %s RETURNING membre_id",
        (payload.decision, user.id, attestation_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown attestation")
    db.execute("UPDATE membre SET attestation_statut = %s WHERE id = %s", (payload.decision, row["membre_id"]), role=user.role)
    audit.log(user.id, user.role, "validation_attestation", "attestation_manuelle", attestation_id, {"decision": payload.decision})
    return {"ok": True, "statut": payload.decision}
