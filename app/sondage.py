"""Admin: send the attendance survey of an activity to its members.

Each recipient gets the activity start localized to THEIR own time zone (a member
in Europe/Paris sees 14:00 for a 12:00 UTC activity) plus the lightweight check-in
link, through the existing notification pipeline (Telegram / e-mail, honouring the
admin toggle, member preferences and language). No new attendance model: the link
lands on the single ``participation`` source of truth, so the survey stays
single-participation and anti-duplicate across every channel.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import channels, db, notifications, temps
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["sondage"])



class SondageIn(BaseModel):
    """Optional explicit recipients; when omitted, all active members are surveyed."""

    membre_ids: list[str] | None = None


def _base_pointage() -> str:
    return channels.integration_value("pointage_externe") or "https://adsum-public.pages.dev"


@router.post("/evenements/{evenement_id}/sondage")
def envoyer_sondage(
    evenement_id: str,
    payload: SondageIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.superviser"))],
) -> dict[str, object]:
    """Send the attendance survey of an activity, localized to each member's zone."""
    ev = db.fetch_one(
        "SELECT id, titre, debut, emargement_externe FROM evenement WHERE id = %s",
        (evenement_id,),
        role=user.role,
    )
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activité introuvable")
    if not ev.get("emargement_externe"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="activez d'abord l'émargement externe sur cette activité")
    lien = f"{_base_pointage()}/?e={evenement_id}"

    if payload.membre_ids:
        rows = db.fetch_all(
            "SELECT id, prenoms, fuseau_horaire FROM membre WHERE id = ANY(%s::uuid[])",
            (payload.membre_ids,),
            role=user.role,
        )
    else:
        rows = db.fetch_all(
            "SELECT id, prenoms, fuseau_horaire FROM membre WHERE statut = 'actif'",
            (),
            role=user.role,
        )

    envoyes = 0
    canaux: set[str] = set()
    sans_canal = 0
    for m in rows:
        quand = temps.formater_instant(ev["debut"], m.get("fuseau_horaire"))
        ctx = {"prenom": m.get("prenoms") or "", "titre": ev["titre"], "quand": quand, "lien": lien}
        used = notifications.notifier(str(m["id"]), user.role, "sondage_activite", ctx, ref_id=str(evenement_id), dedup=False)
        if used:
            envoyes += 1
            canaux.update(used)
        else:
            sans_canal += 1
    return {"cibles": len(rows), "envoyes": envoyes, "canaux": sorted(canaux), "sans_canal": sans_canal, "lien": lien}
