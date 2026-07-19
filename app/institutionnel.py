# ruff: noqa: E501
"""Institutional identity and dates, configurable per organisation.

Organisation identity (name, short name, site, founding date, patron saint and
feast, reference timezone, signature, contact) is stored in ``integration_config``
so nothing organisation-specific is hardcoded and ADSUM can be redeployed for any
association. Institutional dates (anniversary, patron feast, commemorations) are a
CRUD list, each with an optional reminder and a visibility.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import channels, db
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["institutionnel"])

_CLES = [
    "org_nom", "org_nom_court", "org_site", "org_fondation_date",
    "org_saint_patron", "org_saint_patron_date", "org_fuseau", "org_signature", "org_contact",
]


class IdentiteIn(BaseModel):
    org_nom: str = Field(default="", max_length=160)
    org_nom_court: str = Field(default="", max_length=60)
    org_site: str = Field(default="", max_length=300)
    org_fondation_date: str = Field(default="", max_length=20)
    org_saint_patron: str = Field(default="", max_length=160)
    org_saint_patron_date: str = Field(default="", max_length=20)
    org_fuseau: str = Field(default="", max_length=60)
    org_signature: str = Field(default="", max_length=200)
    org_contact: str = Field(default="", max_length=200)


@router.get("/admin/parametres/identite-institutionnelle")
def lire_identite(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, str]:
    """Current organisation identity (editable, never hardcoded in the code)."""
    return {cle: (channels.integration_value(cle) or "") for cle in _CLES}


@router.put("/admin/parametres/identite-institutionnelle", status_code=204)
def maj_identite(payload: IdentiteIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    for cle in _CLES:
        db.execute(
            "INSERT INTO integration_config (cle, valeur, maj_par, maj_le) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
            (cle, str(getattr(payload, cle) or ""), user.id), role=user.role,
        )


# --- Institutional dates ----------------------------------------------------

class DateInstitIn(BaseModel):
    nom: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    type: str = Field(default="commemoration", max_length=40)
    date_fixe: str | None = None
    mois: int | None = Field(default=None, ge=1, le=12)
    jour: int | None = Field(default=None, ge=1, le=31)
    rappel_jours: int = Field(default=7, ge=0, le=365)
    visibilite: str = Field(default="membre", pattern="^(interne|membre|administrateur)$")
    actif: bool = True


def _date_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "nom": r.get("nom"), "description": r.get("description"), "type": r.get("type"),
        "date_fixe": r["date_fixe"].isoformat() if r.get("date_fixe") else None,
        "mois": r.get("mois"), "jour": r.get("jour"), "rappel_jours": r.get("rappel_jours"),
        "visibilite": r.get("visibilite"), "actif": bool(r.get("actif")),
    }


@router.get("/admin/dates-institutionnelles")
def lister_dates(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT * FROM date_institutionnelle ORDER BY COALESCE(mois, 99), COALESCE(jour, 99), nom", (), role=user.role)
    return [_date_dict(r) for r in rows]


@router.post("/admin/dates-institutionnelles")
def creer_date(payload: DateInstitIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    row = db.execute(
        "INSERT INTO date_institutionnelle (nom, description, type, date_fixe, mois, jour, rappel_jours, visibilite, actif) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (payload.nom, payload.description, payload.type, payload.date_fixe or None, payload.mois, payload.jour, payload.rappel_jours, payload.visibilite, payload.actif),
        role=user.role,
    )
    return _date_dict(row or {})


@router.patch("/admin/dates-institutionnelles/{date_id}")
def maj_date(date_id: str, payload: DateInstitIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    row = db.execute(
        "UPDATE date_institutionnelle SET nom=%s, description=%s, type=%s, date_fixe=%s, mois=%s, jour=%s, rappel_jours=%s, visibilite=%s, actif=%s WHERE id=%s RETURNING *",
        (payload.nom, payload.description, payload.type, payload.date_fixe or None, payload.mois, payload.jour, payload.rappel_jours, payload.visibilite, payload.actif, date_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Date introuvable.")
    return _date_dict(row)


@router.delete("/admin/dates-institutionnelles/{date_id}", status_code=204)
def supprimer_date(date_id: str, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    db.execute("DELETE FROM date_institutionnelle WHERE id = %s", (date_id,), role=user.role)


@router.get("/membres/me/identite-institutionnelle")
def identite_membre(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """Public-facing institutional identity for the member app (name, short name,
    site, signature) plus the member-visible institutional dates."""
    identite = {cle: (channels.integration_value(cle) or "") for cle in ("org_nom", "org_nom_court", "org_site", "org_signature", "org_saint_patron")}
    dates = db.fetch_all(
        "SELECT * FROM date_institutionnelle WHERE actif AND visibilite IN ('membre','interne') ORDER BY COALESCE(mois, 99), COALESCE(jour, 99)",
        (), role=user.role,
    )
    return {"identite": identite, "dates": [_date_dict(r) for r in dates]}
