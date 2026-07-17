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


# Child tables referencing a member with a blocking FK (NO ACTION / RESTRICT): they must
# be deleted BEFORE the member, otherwise the final DELETE raises. Every FK that is
# ON DELETE CASCADE / SET NULL is handled by the database and is intentionally absent
# here. comptage_ligne is optional and best-effort (it may not exist on every install).
_CHILD_TABLES = (
    "notification", "document", "presence", "recensement_reponse",
    "engagement", "jeton_qr", "comptage_ligne",
)
# NO ACTION references that must be NULLED (not deleted): the row belongs to another
# entity that survives (e.g. a commission whose responsable is this member).
_NULL_REFS = (("commission", "responsable_id"),)


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


@router.get("/{membre_id}/suppression-apercu")
def apercu_suppression(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> dict[str, object]:
    """Pre-flight for an RGPD erasure: what the member currently holds, so the operator
    sees the impact (titles/functions carried, memberships, responsibilities, records)
    and can choose an alternative (archive/deactivate/suspend) before an irreversible
    deletion, exactly like the level/function deletion resolver."""
    m = db.fetch_one(
        "SELECT prenoms, nom, statut, est_berger, nom_pastoral, fonction_cle FROM membre WHERE id = %s",
        (membre_id,), role=user.role,
    )
    if not m:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")

    def _count(sql: str) -> int:
        try:
            return int((db.fetch_one(sql, (membre_id,)) or {}).get("n") or 0)
        except Exception:  # noqa: BLE001 - an optional table must not break the preview
            return 0

    fonctions = [
        str(r["libelle"]) for r in db.fetch_all(
            "SELECT fh.libelle_h AS libelle FROM membre_fonction mf JOIN fonction_honorifique fh ON fh.cle = mf.fonction_cle "
            "WHERE mf.membre_id = %s AND mf.actif = true ORDER BY mf.principale DESC, mf.ordre",
            (membre_id,),
        ) or []
    ]
    a_un_compte = bool(db.fetch_one("SELECT 1 FROM utilisateur WHERE membre_id = %s", (membre_id,), role=user.role))
    return {
        "membre_id": membre_id,
        "nom": f"{m.get('prenoms') or ''} {m.get('nom') or ''}".strip(),
        "statut": m.get("statut"),
        "porte_titre_berger": bool(m.get("est_berger")),
        "nom_pastoral": m.get("nom_pastoral"),
        "fonctions": fonctions,
        "responsable_de_commissions": _count("SELECT count(*) AS n FROM commission WHERE responsable_id = %s"),
        "appartenances": _count("SELECT count(*) AS n FROM appartenance WHERE membre_id = %s"),
        "espaces_collaboration": _count("SELECT count(*) AS n FROM collab_espace_membre WHERE utilisateur_id IN (SELECT id FROM utilisateur WHERE membre_id = %s)"),
        "participations": _count("SELECT count(*) AS n FROM participation WHERE membre_id = %s"),
        "documents": _count("SELECT count(*) AS n FROM document WHERE membre_id = %s"),
        "a_un_compte": a_un_compte,
        # Reversible alternatives to an irreversible erasure.
        "alternatives": ["archiver", "desactiver", "suspendre"],
    }


class StatutMembreIn(BaseModel):
    statut: str


@router.patch("/{membre_id}/statut")
def changer_statut(
    membre_id: str, payload: StatutMembreIn,
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
) -> dict[str, object]:
    """Reversible lifecycle alternative to erasure: archive / deactivate / suspend /
    reactivate a member. Moves the member to the matching directory compartment without
    destroying any data, and is fully audited."""
    cible = (payload.statut or "").strip()
    if cible not in ("actif", "inactif", "suspendu", "archive"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut invalide")
    row = db.execute("UPDATE membre SET statut = %s WHERE id = %s RETURNING id", (cible, membre_id), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    audit.log(user.id, user.role, "changement_statut_membre", "membre", membre_id, {"statut": cible})
    return {"ok": True, "statut": cible}


@router.delete("/{membre_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_membre(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.administrer"))]) -> None:
    """RGPD right to erasure: purge files then every member record, atomically.

    All blocking child references (NO ACTION FKs) are removed first, and references that
    belong to a surviving entity (a commission's responsable) are nulled, so the final
    DELETE never fails on a real member (e.g. one holding an engagement or a QR token).
    ON DELETE CASCADE / SET NULL references are handled by the database."""
    exists = db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # 1) purge stored files in both private buckets (outside the DB transaction).
    storage.delete_prefix(settings.storage_bucket_photos, f"{membre_id}/")
    storage.delete_prefix(settings.storage_bucket_documents, f"{membre_id}/")
    # 2) one transaction: null the surviving-entity refs, delete the blocking child rows,
    #    then the account and the member. Atomic: a failure rolls the whole erasure back.
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        for table, col in _NULL_REFS:
            cur.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = %s", (membre_id,))
        cur.execute(
            "DELETE FROM demande_message WHERE demande_id IN (SELECT id FROM demande WHERE membre_id = %s)",
            (membre_id,),
        )
        cur.execute("DELETE FROM demande WHERE membre_id = %s", (membre_id,))
        for table in _CHILD_TABLES:
            try:
                cur.execute(f"SAVEPOINT s_{table}")
                cur.execute(f"DELETE FROM {table} WHERE membre_id = %s", (membre_id,))
                cur.execute(f"RELEASE SAVEPOINT s_{table}")
            except Exception:  # noqa: BLE001 - a non-existent optional table must not block erasure
                cur.execute(f"ROLLBACK TO SAVEPOINT s_{table}")
        cur.execute("DELETE FROM utilisateur WHERE membre_id = %s", (membre_id,))
        cur.execute("DELETE FROM membre WHERE id = %s", (membre_id,))
    audit.log(user.id, user.role, "suppression_rgpd_membre", "membre", membre_id, {})
