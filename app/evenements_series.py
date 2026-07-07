"""Recurring-activity series management, split out of admin.py to keep that
module within the file-size policy.

The single endpoint here appends dates to an activity's series in a strictly
additive way: it only inserts new occurrences, promotes a one-off to a series
master on the first append, skips dates already scheduled, and caps the series
length. It never deletes or overwrites an existing occurrence, so occurrences
that already carry attendance are left untouched.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import OccurrenceIn, UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_SERIE_MAX = 104


class AjoutOccurrencesIn(BaseModel):
    """Extra dates to append to an activity's series (additive, never destructive)."""

    occurrences: list[OccurrenceIn] = Field(default_factory=list, max_length=104)


@router.post("/evenements/{evenement_id}/occurrences")
def ajouter_occurrences(
    evenement_id: str,
    payload: AjoutOccurrencesIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, int]:
    """Append dates to an activity's series without touching existing occurrences.

    Each new date becomes one more real activity row sharing the series id, so
    attendance and questionnaire keep working per date. A one-off activity is
    promoted to a series master on the first append. A date already scheduled in
    the series is skipped (no duplicate), and the series is capped so it can never
    grow unbounded. Nothing is ever deleted, so occurrences that already carry
    attendance are left strictly untouched."""
    master = db.fetch_one(
        "SELECT id, serie_id, titre, type, volet, lieu, mode, lien_session, liens, type_diffusion, "
        "visibilite, cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails, "
        "fenetre_reponse_heures, fuseau_horaire FROM evenement WHERE id = %s",
        (evenement_id,),
        role=user.role,
    )
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    serie = str(master["serie_id"]) if master.get("serie_id") else evenement_id
    if not master.get("serie_id"):
        # Promote the one-off to the master of a new series.
        db.execute("UPDATE evenement SET serie_id = %s WHERE id = %s", (serie, evenement_id), role=user.role)
    total = int((db.fetch_one("SELECT count(*) AS n FROM evenement WHERE serie_id = %s", (serie,), role=user.role) or {}).get("n") or 0)
    liens = [str(x) for x in (master.get("liens") or []) if x]
    ajoutees = 0
    for occ in payload.occurrences:
        if total + ajoutees >= _SERIE_MAX:
            break
        # Skip a date already scheduled in the series (idempotent, no duplicates).
        if db.fetch_one("SELECT 1 FROM evenement WHERE serie_id = %s AND debut = %s", (serie, occ.debut), role=user.role):
            continue
        db.execute(
            "INSERT INTO evenement (titre, type, volet, debut, fin, lieu, mode, lien_session, liens, "
            "type_diffusion, visibilite, cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, "
            "cible_emails, fenetre_reponse_heures, fuseau_horaire, serie_id, cree_par) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::text[],%s,%s,%s,%s)",
            (
                master["titre"], master.get("type"), master["volet"], occ.debut, occ.fin, master.get("lieu"),
                occ.mode or master.get("mode"), master.get("lien_session"), json.dumps(liens),
                master.get("type_diffusion") or "aucun", master.get("visibilite") or "membres",
                master.get("cible_type") or "general", master.get("cible_id"), master.get("cible_genre"),
                master.get("cible_age_min"), master.get("cible_age_max"),
                [str(x) for x in (master.get("cible_emails") or [])],
                master.get("fenetre_reponse_heures"), master.get("fuseau_horaire") or "Africa/Abidjan",
                serie, user.id,
            ),
            role=user.role,
        )
        ajoutees += 1
    nouveau_total = total + ajoutees
    # Keep the recorded rule count in sync for display on the series master.
    db.execute(
        "UPDATE evenement SET recurrence = %s::jsonb WHERE id = %s",
        (json.dumps({"freq": "custom", "interval": 1, "count": nouveau_total}), serie),
        role=user.role,
    )
    audit.log(user.id, user.role, "ajout_occurrences_serie", "evenement", serie, {"ajoutees": ajoutees, "total": nouveau_total})
    return {"ajoutees": ajoutees, "total": nouveau_total}
