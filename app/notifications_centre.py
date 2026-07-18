# ruff: noqa: E501
"""Member notification center: categorized, paginated, status-aware reading.

The header bell and the notification page read this module. It classifies each
notification into a stable member-facing category, flags the ones that require an
action, derives a priority, and serves the list with server-side pagination and
filters (status tab, category, month, year, full-text) so the center stays fluid
over years of history. It NEVER destroys the institutional record: a member can
mark unread and hide a notification from their own view (``masque``), and the
retention job archives old ones (``archive``); both stay auditable.

Backward compatible: the legacy ``GET /me/notifications`` (list) and the mark-read
endpoints in ``membres.py`` are untouched. This module only adds richer reads and
two new personal-state actions.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from . import db
from .membres import require_membre

router = APIRouter(prefix="/api/v1/membres/me", tags=["notifications-centre"])

# Single source of the member-facing taxonomy (nine categories from the spec),
# derived deterministically from the notification type. Kept in SQL so filtering
# and pagination happen in the database.
_CATEGORIE_SQL = """
CASE
  WHEN n.type IN ('activite_demarree','activite_modifiee','activite_annulee','activite_test_diffusion') THEN 'activites'
  WHEN n.type IN ('activite_rappel_j1','activite_rappel_start','agenda_hebdo','recap_hebdo','attestation_rappel','rappel') THEN 'rappels'
  WHEN n.type IN ('sondage_activite','questionnaire_disponible','participation','participation_bientot_close') THEN 'sondages'
  WHEN n.type IN ('annonce') THEN 'informations'
  WHEN n.type IN ('demande_reponse','modification_decision','modification_complement','inscription_soumise','inscription_decision','attestation_requise','attestation_expiree','retention_renouvellement','correction_demandee','demande','inscription','engagement','recensement') THEN 'organisation'
  WHEN n.type IN ('document','document_demande') THEN 'documents'
  WHEN n.type IN ('compte_cree','otp','otp_expire','engagement_code','compte_bloque','compte_debloque','connexion_inhabituelle','securite_alerte','telegram_liaison') THEN 'compte_securite'
  ELSE 'systeme'
END
"""
_ACTION_SQL = """
(n.type IN ('correction_demandee','document_demande','attestation_requise','modification_complement','inscription_decision','sondage_activite','questionnaire_disponible','participation_bientot_close','retention_renouvellement','collab_assignation','collab_demande','collab_echeance'))
"""
_PRIORITE_SQL = """
CASE
  WHEN n.type IN ('securite_alerte','connexion_inhabituelle','compte_bloque','activite_annulee','correction_demandee','otp','otp_expire') THEN 'haute'
  WHEN n.type IN ('activite_demarree','sondage_activite','attestation_requise','participation_bientot_close') THEN 'moyenne'
  ELSE 'normale'
END
"""

_ONGLETS = {"toutes", "non_lues", "lues", "actions", "systeme"}
_CATEGORIES = {"activites", "rappels", "sondages", "informations", "organisation", "controles", "documents", "compte_securite", "systeme"}


def _filtre_onglet(onglet: str) -> str:
    if onglet == "non_lues":
        return " AND NOT n.lu"
    if onglet == "lues":
        return " AND n.lu"
    if onglet == "actions":
        return f" AND {_ACTION_SQL}"
    if onglet == "systeme":
        return f" AND {_CATEGORIE_SQL} IN ('systeme','compte_securite')"
    return ""


class NotifCentreItem(BaseModel):
    id: str
    type: str | None
    categorie: str
    priorite: str
    action_requise: bool
    titre: str | None
    corps: str | None
    lu: bool
    lu_le: str | None
    cree_le: str | None


class NotifCentreOut(BaseModel):
    items: list[NotifCentreItem]
    total: int
    non_lus: int
    page: int
    pages: int
    limit: int


@router.get("/notifications/centre", response_model=NotifCentreOut)
def notifications_centre(
    ctx: Annotated[tuple[str, str], Depends(require_membre)],
    onglet: str = Query(default="toutes"),
    categorie: str | None = Query(default=None),
    mois: int | None = Query(default=None, ge=1, le=12),
    annee: int | None = Query(default=None, ge=2020, le=2100),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> NotifCentreOut:
    """Paginated, categorized notifications for the connected member. Archived and
    self-hidden notifications never appear here (they remain in the audit trail)."""
    membre_id, role = ctx
    onglet = onglet if onglet in _ONGLETS else "toutes"
    where = "WHERE n.membre_id = %s AND NOT n.archive AND NOT n.masque"
    params: list[Any] = [membre_id]
    where += _filtre_onglet(onglet)
    if categorie and categorie in _CATEGORIES:
        where += f" AND {_CATEGORIE_SQL} = %s"
        params.append(categorie)
    if mois:
        where += " AND EXTRACT(MONTH FROM n.cree_le) = %s"
        params.append(mois)
    if annee:
        where += " AND EXTRACT(YEAR FROM n.cree_le) = %s"
        params.append(annee)
    if q:
        where += " AND (n.titre ILIKE %s OR n.corps ILIKE %s)"
        like = f"%{q}%"
        params.extend([like, like])

    total_row = db.fetch_one(f"SELECT count(*) AS c FROM notification n {where}", tuple(params), role=role)
    total = int(total_row["c"]) if total_row else 0
    nonlus_row = db.fetch_one(
        "SELECT count(*) AS c FROM notification n WHERE n.membre_id = %s AND NOT n.archive AND NOT n.masque AND NOT n.lu",
        (membre_id,), role=role,
    )
    non_lus = int(nonlus_row["c"]) if nonlus_row else 0

    rows = db.fetch_all(
        f"""
        SELECT n.id, n.type, {_CATEGORIE_SQL} AS categorie, {_PRIORITE_SQL} AS priorite,
               {_ACTION_SQL} AS action_requise, n.titre, n.corps, n.lu, n.lu_le, n.cree_le
        FROM notification n
        {where}
        ORDER BY n.cree_le DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
        role=role,
    )
    items = [
        NotifCentreItem(
            id=str(r["id"]), type=r.get("type"), categorie=str(r["categorie"]), priorite=str(r["priorite"]),
            action_requise=bool(r["action_requise"]), titre=r.get("titre"), corps=r.get("corps"),
            lu=bool(r["lu"]), lu_le=r["lu_le"].isoformat() if r.get("lu_le") else None,
            cree_le=r["cree_le"].isoformat() if r.get("cree_le") else None,
        )
        for r in rows
    ]
    pages = max(1, (total + limit - 1) // limit)
    return NotifCentreOut(items=items, total=total, non_lus=non_lus, page=(offset // limit) + 1, pages=pages, limit=limit)


@router.get("/notifications/compteur")
def notifications_compteur(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> dict[str, int]:
    """Unread badge count (excludes archived and self-hidden)."""
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT count(*) AS c FROM notification WHERE membre_id = %s AND NOT archive AND NOT masque AND NOT lu",
        (membre_id,), role=role,
    )
    return {"non_lus": int(row["c"]) if row else 0}


@router.post("/notifications/{notification_id}/non-lu", status_code=status.HTTP_204_NO_CONTENT)
def marquer_non_lu(notification_id: str, ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> None:
    """Mark a notification back as unread (member choice). Idempotent."""
    membre_id, role = ctx
    db.execute(
        "UPDATE notification SET lu = false, lu_le = NULL WHERE id = %s AND membre_id = %s",
        (notification_id, membre_id), role=role,
    )


@router.post("/notifications/{notification_id}/masquer", status_code=status.HTTP_204_NO_CONTENT)
def masquer(notification_id: str, ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> None:
    """Hide a notification from the member's own view without deleting the record."""
    membre_id, role = ctx
    db.execute(
        "UPDATE notification SET masque = true, masque_le = now() WHERE id = %s AND membre_id = %s",
        (notification_id, membre_id), role=role,
    )
