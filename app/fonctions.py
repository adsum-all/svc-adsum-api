"""Honorific function catalogue: member read, admin management and validation.

Members pick a function at registration (self-service). The honorific prefix is
only shown to others once an administrator has confirmed the member's function,
so an unearned title can never be displayed. The catalogue itself (labels, VIP
flag, ordering) is fully editable by the administration.
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

router = APIRouter(prefix="/api/v1", tags=["fonctions"])

require_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")


class FonctionIn(BaseModel):
    cle: str = Field(min_length=2, max_length=40, pattern="^[a-z0-9_]+$")
    libelle_h: str = Field(min_length=1, max_length=60)
    libelle_f: str = Field(min_length=1, max_length=60)
    libelle_n: str = Field(min_length=1, max_length=60)
    est_vip: bool = False
    ordre: int = 100


class FonctionPatch(BaseModel):
    libelle_h: str | None = Field(default=None, max_length=60)
    libelle_f: str | None = Field(default=None, max_length=60)
    libelle_n: str | None = Field(default=None, max_length=60)
    est_vip: bool | None = None
    ordre: int | None = None
    actif: bool | None = None


class MembreFonctionIn(BaseModel):
    fonction_cle: str | None = None
    confirmee: bool = True


def _row_to_dict(r: dict[str, object]) -> dict[str, object]:
    return {
        "cle": r["cle"], "libelle_h": r["libelle_h"], "libelle_f": r["libelle_f"],
        "libelle_n": r["libelle_n"], "est_vip": bool(r["est_vip"]), "ordre": r["ordre"],
        "actif": bool(r["actif"]),
    }


@router.get("/fonctions")
def catalogue_actif(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """Active catalogue, used to populate the registration select for any member."""
    rows = db.fetch_all(
        "SELECT cle, libelle_h, libelle_f, libelle_n, est_vip, ordre, actif "
        "FROM fonction_honorifique WHERE actif = true ORDER BY ordre, cle",
        (),
        role=user.role,
    )
    return [_row_to_dict(r) for r in rows]


@router.get("/admin/fonctions")
def list_fonctions(user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT cle, libelle_h, libelle_f, libelle_n, est_vip, ordre, actif "
        "FROM fonction_honorifique ORDER BY ordre, cle",
        (),
        role=user.role,
    )
    return [_row_to_dict(r) for r in rows]


@router.post("/admin/fonctions")
def create_fonction(payload: FonctionIn, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    exists = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (payload.cle,), role=user.role)
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="function already exists")
    db.execute(
        "INSERT INTO fonction_honorifique (cle, libelle_h, libelle_f, libelle_n, est_vip, ordre, maj_par, maj_le) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now())",
        (payload.cle, payload.libelle_h, payload.libelle_f, payload.libelle_n, payload.est_vip, payload.ordre, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "creation_fonction", "fonction_honorifique", payload.cle, {})
    return {"ok": True, "cle": payload.cle}


@router.put("/admin/fonctions/{cle}")
def update_fonction(cle: str, payload: FonctionPatch, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE fonction_honorifique SET {sets}, maj_par = %s, maj_le = now() WHERE cle = %s RETURNING cle",
        (*fields.values(), user.id, cle),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    audit.log(user.id, user.role, "maj_fonction", "fonction_honorifique", cle, fields)
    return {"ok": True, "cle": cle}


@router.delete("/admin/fonctions/{cle}")
def retire_fonction(cle: str, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    """Soft delete: keep the row so members already linked are not broken."""
    row = db.execute(
        "UPDATE fonction_honorifique SET actif = false, maj_par = %s, maj_le = now() WHERE cle = %s RETURNING cle",
        (user.id, cle),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown function")
    audit.log(user.id, user.role, "retrait_fonction", "fonction_honorifique", cle, {})
    return {"ok": True, "cle": cle}


@router.put("/admin/membres/{membre_id}/fonction")
def valider_fonction_membre(
    membre_id: str, payload: MembreFonctionIn, user: Annotated[UserMe, Depends(require_writer)]
) -> dict[str, object]:
    """Assign and/or confirm a member's honorific function (admin validation)."""
    if payload.fonction_cle is not None:
        known = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (payload.fonction_cle,), role=user.role)
        if not known:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown function")
    row = db.execute(
        "UPDATE membre SET fonction_cle = COALESCE(%s, fonction_cle), fonction_confirmee = %s WHERE id = %s RETURNING id",
        (payload.fonction_cle, payload.confirmee, membre_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    audit.log(user.id, user.role, "validation_fonction_membre", "membre", membre_id,
              {"fonction_cle": payload.fonction_cle, "confirmee": payload.confirmee})
    return {"ok": True}
