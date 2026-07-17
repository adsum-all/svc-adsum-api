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
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["niveaux"])

# Fine-grained enforcement (iso-behaviour proven by the conformity test):
# consulter = the former STAFF read, gerer = the former writer set.
require_niveaux_lecture = require_permission("niveaux-engagement.consulter")
require_niveaux_gestion = require_permission("niveaux-engagement.gerer")


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
def list_niveaux(user: Annotated[UserMe, Depends(require_niveaux_lecture)]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT cle, libelle, ordre, actif FROM niveau_engagement ORDER BY ordre, libelle", (), role=user.role
    )
    return [_row(r) for r in rows]


@router.post("/admin/niveaux-engagement", status_code=status.HTTP_201_CREATED)
def create_niveau(payload: NiveauIn, user: Annotated[UserMe, Depends(require_niveaux_gestion)]) -> dict[str, object]:
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
def update_niveau(cle: str, payload: NiveauPatch, user: Annotated[UserMe, Depends(require_niveaux_gestion)]) -> dict[str, object]:
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
def deactivate_niveau(cle: str, user: Annotated[UserMe, Depends(require_niveaux_gestion)]) -> dict[str, object]:
    """Soft delete: keep the row so members already on this level keep a label."""
    row = db.execute(
        "UPDATE niveau_engagement SET actif = false WHERE cle = %s RETURNING cle", (cle,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    audit.log(user.id, user.role, "retrait_niveau_engagement", "niveau_engagement", cle, {})
    return {"ok": True, "cle": cle}


class ReaffecterNiveauIn(BaseModel):
    cible_cle: str | None = None


@router.get("/admin/niveaux-engagement/{cle}/dependances")
def dependances_niveau(cle: str, user: Annotated[UserMe, Depends(require_niveaux_lecture)]) -> dict[str, object]:
    """Members currently on this level (there is no FK: the link is membre.type_membre
    by convention), plus the active levels it can be reassigned to, so the operator can
    move them before deactivating instead of leaving members on a dead key."""
    if not db.fetch_one("SELECT cle FROM niveau_engagement WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    n = int((db.fetch_one("SELECT count(*) AS n FROM membre WHERE type_membre = %s", (cle,)) or {}).get("n") or 0)
    ech = [
        str(r["nom"]) for r in db.fetch_all(
            "SELECT nom_affiche AS nom FROM membre WHERE type_membre = %s AND nom_affiche IS NOT NULL "
            "ORDER BY nom_affiche LIMIT 6", (cle,),
        )
    ]
    cibles = [
        {"cle": r["cle"], "nom": r["libelle"]} for r in db.fetch_all(
            "SELECT cle, libelle FROM niveau_engagement WHERE cle <> %s AND actif = true ORDER BY ordre, libelle",
            (cle,), role=user.role,
        )
    ]
    return {"cle": cle, "porteurs": n, "supprimable": n == 0, "echantillon": ech, "cibles": cibles}


@router.post("/admin/niveaux-engagement/{cle}/reaffecter")
def reaffecter_niveau(
    cle: str, payload: ReaffecterNiveauIn, user: Annotated[UserMe, Depends(require_niveaux_gestion)]
) -> dict[str, object]:
    """Move every member on this level to a target active level, or clear the level
    (set membre.type_membre to NULL) when no target is given."""
    if not db.fetch_one("SELECT cle FROM niveau_engagement WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    cible = (payload.cible_cle or "").strip().lower() or None
    if cible:
        if cible == cle:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="la cible doit differer de la source")
        if not db.fetch_one("SELECT cle FROM niveau_engagement WHERE cle = %s AND actif = true", (cible,), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="niveau cible inconnu ou inactif")
    porteurs = int((db.fetch_one("SELECT count(*) AS n FROM membre WHERE type_membre = %s", (cle,)) or {}).get("n") or 0)
    db.execute("UPDATE membre SET type_membre = %s WHERE type_membre = %s", (cible, cle))
    audit.log(user.id, user.role, "reaffectation_niveau_engagement", "niveau_engagement", cle,
              {"cible": cible, "porteurs": porteurs})
    return {"reaffectes": porteurs, "cible_cle": cible}


@router.delete("/admin/niveaux-engagement/{cle}/definitif")
def supprimer_niveau_definitif(cle: str, user: Annotated[UserMe, Depends(require_niveaux_gestion)]) -> dict[str, object]:
    """Definitive HARD delete of a level (distinct from the reversible deactivation).
    There is no FK on the level key (the link is ``membre.type_membre`` by convention),
    so the row is only removed when no member is still on it: otherwise we refuse with
    409 and the operator must reassign or clear the members first (``/reaffecter``).
    Irreversible once done, and fully audited."""
    if not db.fetch_one("SELECT cle FROM niveau_engagement WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown level")
    porteurs = int((db.fetch_one("SELECT count(*) AS n FROM membre WHERE type_membre = %s", (cle,)) or {}).get("n") or 0)
    if porteurs > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Suppression definitive impossible : {porteurs} membre(s) sont encore sur ce niveau. Reaffectez-les ou detachez-les d'abord.",
        )
    db.execute("DELETE FROM niveau_engagement WHERE cle = %s", (cle,), role=user.role)
    audit.log(user.id, user.role, "suppression_definitive_niveau_engagement", "niveau_engagement", cle, {})
    return {"ok": True, "cle": cle, "definitif": True}
