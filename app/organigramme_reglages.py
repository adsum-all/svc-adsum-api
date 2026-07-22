"""Member-facing display settings for the organisation chart, stored in ``parametre``.

The back office decides which tabs of the member "Ma hierarchie" view are visible (by
default only the personal chain and the org chart) and how the published chart is shown
(the interactive canvas, or a simpler image-like view the member just zooms and pans).
Reads are open to any authenticated member; writes require ``organisation.administrer``.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import audit, db
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["organigramme-reglages"])

_CLE_ONGLETS = "organigramme_membre_onglets"
_CLE_AFFICHAGE = "organigramme_membre_affichage"
_ONGLETS = ("chaine", "rattachements", "titres", "organigramme")
# Default: only the personal chain and the org chart. The two others are hidden until
# an administrator turns them on.
_DEFAUT_ONGLETS: dict[str, bool] = {"chaine": True, "rattachements": False, "titres": False, "organigramme": True}
_AFFICHAGES = ("interactif", "image")
_DEFAUT_AFFICHAGE = "interactif"


def _lire(role: str | None) -> dict[str, object]:
    onglets = dict(_DEFAUT_ONGLETS)
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = %s", (_CLE_ONGLETS,), role=role)
    if row and isinstance(row.get("valeur"), dict):
        for k in _ONGLETS:
            if k in row["valeur"]:
                onglets[k] = bool(row["valeur"][k])
    # Safety net: the personal chain is always available, so the view is never empty.
    onglets["chaine"] = True
    aff = db.fetch_one("SELECT valeur FROM parametre WHERE cle = %s", (_CLE_AFFICHAGE,), role=role)
    affichage = aff["valeur"] if (aff and aff.get("valeur") in _AFFICHAGES) else _DEFAUT_AFFICHAGE
    return {"onglets": onglets, "affichage": affichage}


@router.get("/organigramme/reglages")
def reglages_membre(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Display settings consumed by the member app (visible tabs + chart display mode)."""
    return _lire(user.role)


class ReglagesIn(BaseModel):
    onglets: dict[str, bool] | None = None
    affichage: str | None = None


@router.get("/admin/organigramme/reglages")
def reglages_admin(user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, object]:
    return _lire(user.role)


def _upsert(cle: str, valeur: object, user: UserMe) -> None:
    db.execute(
        "INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le) "
        "VALUES (%s, %s::jsonb, 'organigramme', %s, %s, now()) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
        (cle, json.dumps(valeur), "Affichage de l'organigramme côté membre", user.id),
        role=user.role,
    )


@router.put("/admin/organigramme/reglages")
def maj_reglages(payload: ReglagesIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, object]:
    """Update which member tabs are visible and the chart display mode."""
    if payload.onglets is not None:
        onglets = {k: bool(payload.onglets.get(k, _DEFAUT_ONGLETS[k])) for k in _ONGLETS}
        onglets["chaine"] = True
        _upsert(_CLE_ONGLETS, onglets, user)
    if payload.affichage in _AFFICHAGES:
        _upsert(_CLE_AFFICHAGE, payload.affichage, user)
    audit.log(user.id, user.role, "modification_reglages_organigramme", "parametre", _CLE_ONGLETS,
              {"onglets": payload.onglets, "affichage": payload.affichage})
    return _lire(user.role)
