"""Admin edits on the organizational structure: rename, publish, delete.

The four structural entities (coordination, intendance, commission and
sous_commission) can be renamed, published or unpublished, and deleted when
nothing still references them. Publishing controls whether the entity appears in
the member registration dropdowns (see reference.py). Reserved to super_admin and
admin; every change is written to the audit log.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/organisation", tags=["organisation"])


# URL segment -> physical table. The table name is resolved through this fixed
# whitelist and never taken from user input, so it cannot be used for injection.
_TABLES = {
    "coordinations": "coordination",
    "intendances": "intendance",
    "commissions": "commission",
    "groupes": "sous_commission",
}


def _table(entity: str) -> str:
    table = _TABLES.get(entity)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown entity")
    return table


class RenameIn(BaseModel):
    nom: str = Field(min_length=1)


class PublishIn(BaseModel):
    publie: bool


@router.patch("/{entity}/{item_id}")
def rename(
    entity: str,
    item_id: str,
    payload: RenameIn,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, object]:
    """Rename a structural entity."""
    nom = payload.nom.strip()
    if not nom:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")
    table = _table(entity)
    row = db.execute(f"UPDATE {table} SET nom = %s WHERE id = %s RETURNING id, nom", (nom, item_id), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    audit.log(user.id, user.role, "renommage_organisation", entity, item_id, {"nom": nom})
    return {"id": str(row["id"]), "nom": row["nom"]}


@router.patch("/{entity}/{item_id}/publication")
def publication(
    entity: str,
    item_id: str,
    payload: PublishIn,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, object]:
    """Publish or unpublish an entity (controls member dropdown visibility)."""
    table = _table(entity)
    row = db.execute(
        f"UPDATE {table} SET publie = %s WHERE id = %s RETURNING id, publie",
        (payload.publie, item_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    audit.log(user.id, user.role, "publication_organisation", entity, item_id, {"publie": payload.publie})
    return {"id": str(row["id"]), "publie": bool(row["publie"])}


@router.delete("/{entity}/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer(
    entity: str,
    item_id: str,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> None:
    """Delete an entity, refusing if a member or sub-entity still references it."""
    table = _table(entity)
    exists = db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (item_id,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        db.execute(f"DELETE FROM {table} WHERE id = %s", (item_id,), role=user.role)
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="entity still referenced; unpublish it instead of deleting",
        ) from exc
    audit.log(user.id, user.role, "suppression_organisation", entity, item_id, {})
