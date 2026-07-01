"""RGPD member rights: data access (export) and erasure request.

The member downloads a full copy of their personal data (right to access and
portability) and can file an erasure request that the administration processes
(the actual purge is the admin-side operation in gestion.py). Every export and
request is written to the audit log.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, db
from .auth import current_user
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["rgpd"])


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


def _rows(sql: str, membre_id: str, role: str) -> list[dict[str, object]]:
    rows = db.fetch_all(sql, (membre_id,), role=role)
    out: list[dict[str, object]] = []
    for r in rows:
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()})
    return out


@router.get("/export")
def export_donnees(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Full personal-data export (RGPD right to access and portability)."""
    membre_id, role = ctx
    profil = db.fetch_one("SELECT * FROM membre WHERE id = %s", (membre_id,), role=role)
    if not profil:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    profil_clean = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in profil.items()}
    export = {
        "profil": profil_clean,
        "documents": _rows("SELECT type, nom_fichier, statut, demande_le, recu_le FROM document WHERE membre_id = %s", membre_id, role),
        "presences": _rows("SELECT evenement_id, arrivee, depart, methode FROM presence WHERE membre_id = %s ORDER BY arrivee DESC", membre_id, role),
        "demandes": _rows("SELECT type, sujet, statut, cree_le FROM demande WHERE membre_id = %s ORDER BY cree_le DESC", membre_id, role),
        "notifications": _rows("SELECT type, titre, corps, lu, cree_le FROM notification WHERE membre_id = %s ORDER BY cree_le DESC", membre_id, role),
        "connexions": _rows(
            "SELECT s.ip::text AS ip, s.appareil, s.cree_le FROM session s JOIN utilisateur u ON u.id = s.utilisateur_id WHERE u.membre_id = %s ORDER BY s.cree_le DESC LIMIT 100",
            membre_id,
            role,
        ),
    }
    audit.log(None, role, "export_donnees_rgpd", "membre", membre_id, {})
    return export


@router.post("/suppression", status_code=status.HTTP_201_CREATED)
def demander_suppression(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """File an erasure request (RGPD right to be forgotten) for the administration."""
    membre_id, role = ctx
    existing = db.fetch_one(
        "SELECT id FROM demande WHERE membre_id = %s AND type = 'autre' AND sujet = %s AND statut NOT IN ('resolue', 'refusee')",
        (membre_id, "Demande de suppression de compte (RGPD)"),
        role=role,
    )
    if existing:
        return {"ok": True, "deja_demandee": True}
    created = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, statut) VALUES (%s, 'autre', %s, 'ouverte') RETURNING id",
        (membre_id, "Demande de suppression de compte (RGPD)"),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="request not created")
    did = str(created["id"])
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'membre', 'Membre', %s)",
        (did, "Je demande la suppression de mon compte et de mes données personnelles (droit à l'effacement, RGPD)."),
        role=role,
    )
    audit.log(None, role, "demande_suppression_rgpd", "membre", membre_id, {"demande_id": did})
    return {"ok": True, "demande_id": did}
