"""Engagement-level catalogue: admin CRUD and a member-facing read.

The administration owns the levels (create, rename, reorder, deactivate) without
any code change. A level is soft-deactivated, never hard-deleted, so members
already on it keep a readable label. Writes are reserved to writers; the active
catalogue is readable by any authenticated user to populate selects and labels.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .auth import current_user
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["niveaux"])

require_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")


class NiveauIn(BaseModel):
    cle: str = Field(min_length=2, max_length=40, pattern="^[a-z][a-z0-9_]{1,39}$")
    libelle: str = Field(min_length=1, max_length=60)
    ordre: int = Field(default=100, ge=0, le=9999)


class NiveauPatch(BaseModel):
    libelle: str | None = Field(default=None, max_length=60)
    ordre: int | None = Field(default=None, ge=0, le=9999)
    actif: bool | None = None


def _row(r: dict[str, object]) -> dict[str, object]:
    return {"cle": r["cle"], "libelle": r["libelle"], "ordre": r["ordre"], "actif": bool(r["actif"])}


@router.get("/niveaux-engagement")
def catalogue_actif(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """Active levels, ordered hierarchically, for member selects and labels."""
    rows = db.fetch_all(
        "SELECT cle, libelle, ordre, actif FROM niveau_engagement WHERE actif = true ORDER BY ordre, libelle",
        (),
        role=user.role,
    )
    return [_row(r) for r in rows]


@router.get("/admin/niveaux-engagement")
def list_niveaux(user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT cle, libelle, ordre, actif FROM niveau_engagement ORDER BY ordre, libelle", (), role=user.role
    )
    return [_row(r) for r in rows]


@router.post("/admin/niveaux-engagement", status_code=status.HTTP_201_CREATED)
def create_niveau(payload: NiveauIn, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    exists = db.fetch_one("SELECT cle FROM niveau_engagement WHERE cle = %s", (payload.cle,), role=user.role)
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="level already exists")
    db.execute(
        "INSERT INTO niveau_engagement (cle, libelle, ordre) VALUES (%s, %s, %s)",
        (payload.cle, payload.libelle, payload.ordre),
        role=user.role,
    )
    audit.log(user.id, user.role, "creation_niveau_engagement", "niveau_engagement", payload.cle, {"libelle": payload.libelle})
    return {"ok": True, "cle": payload.cle}


@router.put("/admin/niveaux-engagement/{cle}")
def update_niveau(cle: str, payload: NiveauPatch, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE niveau_engagement SET {sets} WHERE cle = %s RETURNING cle",
        (*fields.values(), cle),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    audit.log(user.id, user.role, "maj_niveau_engagement", "niveau_engagement", cle, fields)
    return {"ok": True, "cle": cle}


@router.delete("/admin/niveaux-engagement/{cle}")
def deactivate_niveau(cle: str, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    """Soft delete: keep the row so members already on this level keep a label."""
    row = db.execute(
        "UPDATE niveau_engagement SET actif = false WHERE cle = %s RETURNING cle", (cle,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    audit.log(user.id, user.role, "retrait_niveau_engagement", "niveau_engagement", cle, {})
    return {"ok": True, "cle": cle}
