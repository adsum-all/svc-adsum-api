"""Admin member management: suspend, restore, request a document, RGPD erasure.

Erasure performs a complete purge: the member's child records and their files in
both private Supabase buckets, then the account and the member row. Every action
is written to the audit log. Reserved to admin roles.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db, storage
from .config import settings
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/membres", tags=["gestion"])


# Child tables that reference a member, deleted before the member on erasure.
_CHILD_TABLES = ("notification", "document", "presence", "recensement_reponse", "comptage_ligne")


def _notify(membre_id: str, role: str, titre: str, corps: str) -> None:
    db.execute(
        "INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le) VALUES (%s, 'admin', %s, %s, false, now())",
        (membre_id, titre, corps),
        role=role,
    )


def _notifier_membre(membre_id: str, role: str, type_cle: str, ctx: dict[str, object]) -> None:
    """Multi-channel catalogue notification (in-app + e-mail + Telegram), with the
    member's prenom prefilled. Best-effort: never breaks the admin action."""
    try:
        from .notifications import notifier

        row = db.fetch_one("SELECT prenoms FROM membre WHERE id = %s", (membre_id,), role=role)
        prenom = (str((row or {}).get("prenoms") or "").split(" ")[0]) or "cher membre"
        notifier(membre_id, role, type_cle, {"prenom": prenom, **ctx})
    except Exception:  # noqa: BLE001 - a notification must never break the action
        pass


@router.post("/{membre_id}/bloquer")
def bloquer(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> dict[str, object]:
    row = db.execute("UPDATE membre SET statut = 'suspendu' WHERE id = %s RETURNING email", (membre_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    db.execute("UPDATE utilisateur SET actif = false WHERE membre_id = %s", (membre_id,), role=user.role)
    audit.log(user.id, user.role, "blocage_membre", "membre", membre_id, {})
    _notifier_membre(membre_id, user.role, "compte_bloque", {"motif": "décision administrative"})
    return {"ok": True, "statut": "suspendu"}


@router.post("/{membre_id}/debloquer")
def debloquer(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> dict[str, object]:
    row = db.execute("UPDATE membre SET statut = 'actif' WHERE id = %s RETURNING email", (membre_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    db.execute("UPDATE utilisateur SET actif = true WHERE membre_id = %s", (membre_id,), role=user.role)
    audit.log(user.id, user.role, "deblocage_membre", "membre", membre_id, {})
    _notifier_membre(membre_id, user.role, "compte_debloque", {})
    return {"ok": True, "statut": "actif"}


class DemandeDocIn(BaseModel):
    type: str
    message: str | None = None


@router.post("/{membre_id}/demander-document")
def demander_document(membre_id: str, payload: DemandeDocIn, user: Annotated[UserMe, Depends(require_permission("membres.gerer"))]) -> dict[str, object]:
    db.execute(
        "INSERT INTO document (membre_id, type, statut, demande_le) VALUES (%s, %s, 'demande', now())",
        (membre_id, payload.type),
        role=user.role,
    )
    _notify(membre_id, user.role, "Document demandé", payload.message or f"L'administration vous demande une pièce : {payload.type}.")
    audit.log(user.id, user.role, "demande_document", "membre", membre_id, {"type": payload.type})
    return {"ok": True}


@router.get("/{membre_id}/connexions")
def connexions(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> list[dict[str, object]]:
    """Recent login sessions for the member (security tracking).

    Exposes the member's IP and geolocation history, so it is reserved to the
    account administrators, not the general member-managing staff (gestionnaire).
    """
    rows = db.fetch_all(
        "SELECT s.ip::text AS ip, s.appareil, s.pays, s.ville, s.region, s.cree_le, s.fin, s.revoque, "
        "EXTRACT(EPOCH FROM (COALESCE(s.fin, now()) - s.cree_le))::bigint AS duree_s "
        "FROM session s JOIN utilisateur u ON u.id = s.utilisateur_id "
        "WHERE u.membre_id = %s ORDER BY s.cree_le DESC LIMIT 50",
        (membre_id,),
        role=user.role,
    )
    return [
        {
            "ip": r["ip"],
            "appareil": r["appareil"],
            "pays": r.get("pays"),
            "ville": r.get("ville"),
            "region": r.get("region"),
            "cree_le": r["cree_le"].isoformat() if r["cree_le"] else None,
            "fin": r["fin"].isoformat() if r.get("fin") else None,
            "duree_s": int(r["duree_s"]) if r.get("duree_s") is not None else None,
            "revoque": bool(r["revoque"]),
        }
        for r in rows
    ]


@router.delete("/{membre_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_membre(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> None:
    """RGPD right to erasure: purge files then all member records."""
    exists = db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # 1) purge stored files in both private buckets.
    storage.delete_prefix(settings.storage_bucket_photos, f"{membre_id}/")
    storage.delete_prefix(settings.storage_bucket_documents, f"{membre_id}/")
    # 2) delete child records (best-effort: a missing table is ignored).
    db.execute(
        "DELETE FROM demande_message WHERE demande_id IN (SELECT id FROM demande WHERE membre_id = %s)",
        (membre_id,),
        role=user.role,
    )
    db.execute("DELETE FROM demande WHERE membre_id = %s", (membre_id,), role=user.role)
    for table in _CHILD_TABLES:
        try:
            db.execute(f"DELETE FROM {table} WHERE membre_id = %s", (membre_id,), role=user.role)
        except Exception:  # noqa: BLE001 - a non-existent optional table must not block erasure
            pass
    # 3) delete the account and the member.
    db.execute("DELETE FROM utilisateur WHERE membre_id = %s", (membre_id,), role=user.role)
    db.execute("DELETE FROM membre WHERE id = %s", (membre_id,), role=user.role)
    audit.log(user.id, user.role, "suppression_rgpd_membre", "membre", membre_id, {})
