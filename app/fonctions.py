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

from . import audit, db, fonctions_membre
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
        if payload.fonction_cle.strip().lower() in fonctions_membre.TITRES_INTERDITS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Berger/Bergère est un titre de consécration, pas une fonction : gérez-le dans le titre de consécration.")
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


# --- Multiple functions per member -----------------------------------------

class MembreFonctionCreate(BaseModel):
    fonction_cle: str = Field(min_length=2, max_length=40, pattern="^[a-z0-9_]+$")
    perimetre: str | None = Field(default=None, max_length=120)
    confirmee: bool = True
    principale: bool = False
    ordre: int = Field(default=100, ge=0, le=9999)


class MembreFonctionUpdate(BaseModel):
    perimetre: str | None = Field(default=None, max_length=120)
    confirmee: bool | None = None
    actif: bool | None = None
    principale: bool | None = None
    ordre: int | None = Field(default=None, ge=0, le=9999)


def _genre(membre_id: str, role: str) -> object:
    row = db.fetch_one("SELECT genre FROM membre WHERE id = %s", (membre_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return row.get("genre")


@router.get("/admin/membres/{membre_id}/fonctions")
def list_membre_fonctions(membre_id: str, user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    """All functions held by a member (active or ended, confirmed or not)."""
    genre = _genre(membre_id, user.role)
    return fonctions_membre.fonctions_admin(membre_id, genre, user.role)


@router.post("/admin/membres/{membre_id}/fonctions", status_code=status.HTTP_201_CREATED)
def add_membre_fonction(
    membre_id: str, payload: MembreFonctionCreate, user: Annotated[UserMe, Depends(require_writer)]
) -> dict[str, object]:
    """Add a function to a member (a member may hold several). Berger/Bergere is a
    consecration title and is refused here."""
    cle = payload.fonction_cle.strip()
    if cle.lower() in fonctions_membre.TITRES_INTERDITS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Berger/Bergère est un titre de consécration, pas une fonction : gérez-le dans le titre de consécration.")
    known = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s", (cle,), role=user.role)
    if not known:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown function")
    _genre(membre_id, user.role)  # ensures the member exists
    if payload.principale:
        db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (membre_id,), role=user.role)
    db.execute(
        "INSERT INTO membre_fonction (membre_id, fonction_cle, perimetre, confirmee, principale, ordre) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (membre_id, cle, payload.perimetre, payload.confirmee, payload.principale, payload.ordre),
        role=user.role,
    )
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "ajout_fonction_membre", "membre", membre_id, {"fonction_cle": cle, "perimetre": payload.perimetre})
    return {"ok": True}


@router.patch("/admin/membres/{membre_id}/fonctions/{fonction_id}")
def update_membre_fonction(
    membre_id: str, fonction_id: str, payload: MembreFonctionUpdate,
    user: Annotated[UserMe, Depends(require_writer)],
) -> dict[str, object]:
    """Change a member's function (scope, confirmation, active state, primary, order)."""
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return {"ok": True}
    # Confirm the (function, member) pair exists before touching the shared
    # ``principale`` flag, so a mismatched id never wipes every primary marker.
    exists = db.fetch_one(
        "SELECT id FROM membre_fonction WHERE id = %s AND membre_id = %s",
        (fonction_id, membre_id),
        role=user.role,
    )
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    if fields.get("principale") is True:
        db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (membre_id,), role=user.role)
    sets = ", ".join(f"{k} = %s" for k in fields)
    row = db.execute(
        f"UPDATE membre_fonction SET {sets}, maj_le = now() WHERE id = %s AND membre_id = %s RETURNING id",
        (*fields.values(), fonction_id, membre_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "modification_fonction_membre", "membre", membre_id, {"champs": list(fields)})
    return {"ok": True}


@router.delete("/admin/membres/{membre_id}/fonctions/{fonction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_membre_fonction(
    membre_id: str, fonction_id: str, user: Annotated[UserMe, Depends(require_writer)]
) -> None:
    """Remove a function from a member."""
    row = db.execute(
        "DELETE FROM membre_fonction WHERE id = %s AND membre_id = %s RETURNING id",
        (fonction_id, membre_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="function not found")
    fonctions_membre.sync_principale(membre_id, user.role)
    audit.log(user.id, user.role, "retrait_fonction_membre", "membre", membre_id, {"fonction_id": fonction_id})
