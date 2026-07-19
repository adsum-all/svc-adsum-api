# ruff: noqa: E501 - long SQL statements
"""Custom board templates for the collaboration app.

A team can build its own reusable board template (columns with a name, a colour and an
optional WIP limit), scoped to a workspace, on top of the built-in catalogue. Creating,
editing or deleting a template requires the dedicated ``collaboration.modeles`` permission
(granted to super_admin/admin, or to a member through a governance group), plus an active
role in the workspace. Reading the merged catalogue only needs ``collaboration.superviser``
like the built-in one. Deleting a template never touches boards already created from it:
the columns are copied at board creation, never referenced afterwards.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import audit, db
from .collaboration_espaces import GERANTS, require_espace_role
from .fields import LineStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-modeles"])

_MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ModeleColonneIn(BaseModel):
    nom: LineStr
    couleur: str | None = None
    wip: int | None = Field(default=None, ge=1, le=999)

    @field_validator("couleur")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _HEX.match(v):
            raise ValueError("couleur invalide (format attendu #rrggbb)")
        return v


class ModelePersoIn(BaseModel):
    espace_id: str
    nom: TitleStr
    description: TextStr = ""
    visibilite: str = Field(default="espace", pattern="^(espace|prive)$")
    colonnes: list[ModeleColonneIn] = Field(min_length=1, max_length=12)


class ModelePersoPatch(BaseModel):
    nom: TitleStr | None = None
    description: TextStr | None = None
    visibilite: str | None = Field(default=None, pattern="^(espace|prive)$")
    colonnes: list[ModeleColonneIn] | None = Field(default=None, min_length=1, max_length=12)


def _structure(colonnes: list[ModeleColonneIn]) -> str:
    return json.dumps([{"nom": c.nom, "couleur": c.couleur, "wip": c.wip} for c in colonnes])


def _out(r: dict[str, Any]) -> dict[str, Any]:
    cols = r.get("structure")
    if isinstance(cols, str):
        cols = json.loads(cols or "[]")
    return {
        "id": str(r["id"]), "espace_id": str(r["espace_id"]), "libelle": r.get("nom"),
        "description": r.get("description") or "", "visibilite": r.get("visibilite"),
        "cree_par": str(r["cree_par"]) if r.get("cree_par") else None,
        "source": "personnalise",
        "colonnes": [{"nom": c.get("nom"), "couleur": c.get("couleur"), "wip": c.get("wip")} for c in (cols or [])],
    }


def modeles_perso_visibles(espace_id: str, user: UserMe) -> list[dict[str, Any]]:
    """Custom templates a member may reuse in this workspace: the space-wide ones plus
    the caller's own private ones. Used by the merged catalogue."""
    rows = db.fetch_all(
        "SELECT id, espace_id, nom, description, structure, visibilite, cree_par FROM collab_modele_perso "
        "WHERE espace_id = %s AND supprime_le IS NULL AND (visibilite = 'espace' OR cree_par = %s) "
        "ORDER BY nom",
        (espace_id, user.id), role=user.role,
    )
    return [_out(r) for r in rows]


def structure_perso(modele_id: str, espace_id: str, user: UserMe) -> list[dict[str, Any]] | None:
    """Columns of a custom template, if the caller may use it in this workspace. Returns
    None when the id is not a usable custom template (so the caller falls back cleanly)."""
    try:
        uuid.UUID(modele_id)
    except ValueError:
        return None
    row = db.fetch_one(
        "SELECT structure FROM collab_modele_perso WHERE id = %s AND espace_id = %s AND supprime_le IS NULL "
        "AND (visibilite = 'espace' OR cree_par = %s)",
        (modele_id, espace_id, user.id), role=user.role,
    )
    if not row:
        return None
    cols = row["structure"]
    if isinstance(cols, str):
        cols = json.loads(cols or "[]")
    return [{"nom": c.get("nom"), "couleur": c.get("couleur"), "wip": c.get("wip")} for c in (cols or [])]


def _modele_ou_404(modele_id: str, user: UserMe) -> dict[str, Any]:
    # A private template is visible (and thus manageable) only by its author: even a
    # workspace manager must never read or edit another member's private template.
    row = db.fetch_one(
        "SELECT * FROM collab_modele_perso WHERE id = %s AND supprime_le IS NULL "
        "AND (visibilite = 'espace' OR cree_par = %s)",
        (modele_id, user.id), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="modèle introuvable")
    return row


@router.get("/modeles-perso")
def list_modeles_perso(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> list[dict[str, Any]]:
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return modeles_perso_visibles(espace_id, user)


@router.post("/modeles-perso", status_code=status.HTTP_201_CREATED)
def create_modele_perso(
    payload: ModelePersoIn, user: Annotated[UserMe, Depends(require_permission("collaboration.modeles"))],
) -> dict[str, Any]:
    require_espace_role(payload.espace_id, user, _MEMBRES_ACTIFS)
    row = db.execute(
        "INSERT INTO collab_modele_perso (espace_id, nom, description, structure, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, %s) RETURNING *",
        (payload.espace_id, payload.nom, payload.description, _structure(payload.colonnes), payload.visibilite, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_modele_perso_creation", "collab_modele_perso", str((row or {}).get("id")), {"nom": payload.nom, "espace_id": payload.espace_id})
    return _out(row or {})


@router.patch("/modeles-perso/{modele_id}")
def update_modele_perso(
    modele_id: str, payload: ModelePersoPatch, user: Annotated[UserMe, Depends(require_permission("collaboration.modeles"))],
) -> dict[str, Any]:
    modele = _modele_ou_404(modele_id, user)
    espace_id = str(modele["espace_id"])
    # The template's owner may edit their own; otherwise a workspace manager may edit a
    # space-wide template. Never let an arbitrary collaboration.modeles holder edit any.
    if str(modele.get("cree_par")) != user.id:
        require_espace_role(espace_id, user, GERANTS)
    else:
        require_espace_role(espace_id, user, _MEMBRES_ACTIFS)
    champs = payload.model_dump(exclude_unset=True)
    sets: list[str] = []
    vals: list[Any] = []
    if "nom" in champs and payload.nom is not None:
        sets.append("nom = %s")
        vals.append(payload.nom)
    if "description" in champs and payload.description is not None:
        sets.append("description = %s")
        vals.append(payload.description)
    if "visibilite" in champs and payload.visibilite is not None:
        sets.append("visibilite = %s")
        vals.append(payload.visibilite)
    if "colonnes" in champs and payload.colonnes is not None:
        sets.append("structure = %s::jsonb")
        vals.append(_structure(payload.colonnes))
    if not sets:
        return _out(modele)
    vals.append(modele_id)
    row = db.execute(f"UPDATE collab_modele_perso SET {', '.join(sets)}, maj_le = now() WHERE id = %s RETURNING *", tuple(vals), role=user.role)
    audit.log(user.id, user.role, "collab_modele_perso_maj", "collab_modele_perso", modele_id, {"champs": list(champs.keys())})
    return _out(row or {})


@router.delete("/modeles-perso/{modele_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_modele_perso(
    modele_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.modeles"))],
) -> None:
    modele = _modele_ou_404(modele_id, user)
    espace_id = str(modele["espace_id"])
    if str(modele.get("cree_par")) != user.id:
        require_espace_role(espace_id, user, GERANTS)
    else:
        require_espace_role(espace_id, user, _MEMBRES_ACTIFS)
    # Soft-delete: boards already created from this template keep their (copied) columns.
    db.execute("UPDATE collab_modele_perso SET supprime_le = now() WHERE id = %s", (modele_id,), role=user.role)
    audit.log(user.id, user.role, "collab_modele_perso_suppression", "collab_modele_perso", modele_id, {"nom": modele.get("nom")})
