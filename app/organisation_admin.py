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
    "tribus": "tribu",
}


def _table(entity: str) -> str:
    table = _TABLES.get(entity)
    if table is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown entity")
    return table


# Incoming references per physical table, as (referencing table, column, human label).
# Most FKs are ON DELETE SET NULL / CASCADE, so a raw DELETE would silently orphan
# members and sub-structures instead of raising: we count references explicitly and
# refuse the deletion, pointing the operator to deactivation (unpublish) instead.
# Table/column names are fixed internal constants (never user input): no injection.
_REFS: dict[str, list[tuple[str, str, str]]] = {
    "coordination": [
        ("membre", "coordination_id", "membre(s)"),
        ("intendance", "coordination_id", "intendance(s)"),
        ("coordination", "parent_id", "sous-coordination(s)"),
    ],
    "intendance": [
        ("membre", "intendance_id", "membre(s)"),
        ("intendance", "parent_id", "sous-intendance(s)"),
    ],
    "commission": [
        ("membre", "commission_id", "membre(s)"),
        ("sous_commission", "commission_id", "sous-commission(s)"),
        ("appartenance", "commission_id", "rattachement(s)"),
    ],
    "sous_commission": [],
    "tribu": [
        ("membre", "tribu_id", "membre(s)"),
        ("tribu_patriarche_historique", "tribu_id", "historique(s) de patriarche"),
    ],
}

# Reference columns that are NOT NULL and therefore cannot be detached by setting NULL:
# when the operator detaches (no reassignment target), the referencing row is DELETED
# instead. An ``appartenance`` is a member-to-commission link; detaching it from a
# commission that is being removed simply voids that link. Without this, "détacher puis
# supprimer" hit a NOT NULL violation (a 500 surfaced as a network error in the UI).
_DETACH_DELETES: set[tuple[str, str]] = {
    ("appartenance", "commission_id"),
    ("tribu_patriarche_historique", "tribu_id"),
}


def _references(table: str, item_id: str) -> list[str]:
    """Count, at system level (role=None so no perimeter RLS hides a reference),
    everything that still points to this entity. Returns human labels like
    '3 membre(s)' for each non-empty reference, so the refusal is explicit."""
    labels: list[str] = []
    for ref_table, col, label in _REFS.get(table, []):
        row = db.fetch_one(f"SELECT count(*) AS n FROM {ref_table} WHERE {col} = %s", (item_id,))
        cnt = int((row or {}).get("n") or 0)
        if cnt:
            labels.append(f"{cnt} {label}")
    return labels


_NAME_COL = {
    "membre": "nom_affiche",
    "sous_commission": "nom",
    "intendance": "nom",
    "coordination": "nom",
    "tribu": "nom",
}


def _echantillon(ref_table: str, col: str, item_id: str, limite: int = 6) -> list[str]:
    """A few readable names of the dependents, for the resolution UI. System-level
    (role=None) so a perimeter policy never hides a dependent from the operator."""
    name_col = _NAME_COL.get(ref_table)
    if not name_col:
        return []
    rows = db.fetch_all(
        f"SELECT {name_col} AS n FROM {ref_table} WHERE {col} = %s AND {name_col} IS NOT NULL "
        f"ORDER BY {name_col} LIMIT %s",
        (item_id, limite),
    )
    return [str(r["n"]) for r in rows if r.get("n")]


class RenameIn(BaseModel):
    nom: str | None = Field(default=None, min_length=1)
    # Optional details, applied only where the table supports them (see _EDITABLE):
    # description for coordination/intendance/commission, commission_id (parent) for a
    # sous-commission. A field left unset is not touched; description = "" clears it.
    description: str | None = None
    commission_id: str | None = None


# Per-table columns that the edit endpoint may write, beyond the always-allowed nom.
# The column names are fixed internal constants, never user input: no injection.
_EDITABLE: dict[str, set[str]] = {
    "coordination": {"description"},
    "intendance": {"description"},
    "commission": {"description"},
    "sous_commission": {"commission_id"},
    "tribu": {"description"},
}


class ReaffecterIn(BaseModel):
    # Target entity of the SAME table to move the dependents to, or null to detach
    # them (set the reference to NULL). Only same-type targets make sense.
    cible_id: str | None = None


class PublishIn(BaseModel):
    publie: bool


def _edit_sets(table: str, payload: RenameIn, fields: dict[str, object], role: str) -> tuple[list[str], list[object]]:
    """Build the whitelisted SET clause for an entity edit. Only fields the caller sent
    AND the table supports are written; raises 400 on an empty name or an unknown parent."""
    sets: list[str] = []
    params: list[object] = []
    editable = _EDITABLE.get(table, set())
    if "nom" in fields:
        nom = (payload.nom or "").strip()
        if not nom:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")
        sets.append("nom = %s")
        params.append(nom)
    if "description" in fields and "description" in editable:
        sets.append("description = %s")
        params.append((payload.description or "").strip() or None)
    if "commission_id" in fields and "commission_id" in editable:
        cible = (payload.commission_id or "").strip() or None
        if cible and not db.fetch_one("SELECT id FROM commission WHERE id = %s", (cible,), role=role):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="commission de rattachement introuvable",
            )
        sets.append("commission_id = %s")
        params.append(cible)
    return sets, params


@router.patch("/{entity}/{item_id}")
def rename(
    entity: str,
    item_id: str,
    payload: RenameIn,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, object]:
    """Rename and/or edit the details of a structural entity: the name, the description
    (coordination/intendance/commission) and the parent commission of a sous-commission.
    Only fields the caller sent AND the table supports are written."""
    table = _table(entity)
    fields = payload.model_dump(exclude_unset=True)
    sets, params = _edit_sets(table, payload, fields, user.role)
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    params.append(item_id)
    try:
        row = db.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s RETURNING id, nom", tuple(params), role=user.role
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ce nom est deja utilise") from exc
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    audit.log(user.id, user.role, "modification_organisation", entity, item_id, {"champs": sorted(fields)})
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
    """Delete an entity, refusing if a member or sub-entity still references it.

    The refusal is enforced by an explicit reference count (the incoming FKs are
    SET NULL / CASCADE, so a raw DELETE would silently orphan data rather than
    raise). When still referenced, the operator is told exactly what is attached
    and directed to deactivation (unpublish), which already exists."""
    table = _table(entity)
    exists = db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (item_id,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    attaches = _references(table, item_id)
    if attaches:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Suppression impossible : " + ", ".join(attaches)
                + " encore rattaché(s). Désactivez cette unité (dépublier) ou réaffectez d'abord les membres."
            ),
        )
    try:
        db.execute(f"DELETE FROM {table} WHERE id = %s", (item_id,), role=user.role)
    except psycopg.errors.ForeignKeyViolation as exc:
        # Last-resort net for any reference not covered above (should not happen).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Suppression impossible : cette unité est encore utilisée. Désactivez-la au lieu de la supprimer.",
        ) from exc
    audit.log(user.id, user.role, "suppression_organisation", entity, item_id, {})


@router.get("/{entity}/{item_id}/dependances")
def dependances(
    entity: str,
    item_id: str,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, object]:
    """Pre-flight for a deletion: what still references this entity (count + a sample
    of names) and the possible reassignment targets (other entities of the same type),
    so the operator can resolve the dependencies BEFORE deleting instead of hitting a
    dead-end 409."""
    table = _table(entity)
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (item_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    refs: list[dict[str, object]] = []
    total = 0
    for ref_table, col, label in _REFS.get(table, []):
        row = db.fetch_one(f"SELECT count(*) AS n FROM {ref_table} WHERE {col} = %s", (item_id,))
        cnt = int((row or {}).get("n") or 0)
        total += cnt
        if cnt:
            refs.append({
                "table": ref_table, "colonne": col, "label": label, "nombre": cnt,
                "echantillon": _echantillon(ref_table, col, item_id),
            })
    cibles = [
        {"id": str(r["id"]), "nom": r["nom"]}
        for r in db.fetch_all(
            f"SELECT id, nom FROM {table} WHERE id <> %s ORDER BY nom", (item_id,), role=user.role
        )
    ]
    return {
        "entity": entity, "item_id": item_id, "total": total, "supprimable": total == 0,
        "references": refs, "cibles": cibles,
    }


@router.post("/{entity}/{item_id}/reaffecter")
def reaffecter(
    entity: str,
    item_id: str,
    payload: ReaffecterIn,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, object]:
    """Move every dependent of this entity (members, sub-entities, RBAC scopings) to a
    target entity of the same type, or detach them (NULL). System-level updates so no
    perimeter policy leaves a reference behind, which is what unblocks the deletion.
    Transactional: all references move in one request."""
    table = _table(entity)
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (item_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    cible = (payload.cible_id or "").strip() or None
    if cible:
        if cible == item_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="la cible doit differer de la source")
        if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (cible,), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible introuvable")
    reaffectes = 0
    detail: dict[str, int] = {}
    for ref_table, col, _label in _REFS.get(table, []):
        if cible is None and (ref_table, col) in _DETACH_DELETES:
            # NOT NULL reference: detaching means deleting the now-meaningless link row.
            row = db.execute(
                f"WITH u AS (DELETE FROM {ref_table} WHERE {col} = %s RETURNING 1) "
                "SELECT count(*) AS n FROM u",
                (item_id,),
            )
        else:
            row = db.execute(
                f"WITH u AS (UPDATE {ref_table} SET {col} = %s WHERE {col} = %s RETURNING 1) "
                "SELECT count(*) AS n FROM u",
                (cible, item_id),
            )
        n = int((row or {}).get("n") or 0)
        if n:
            detail[ref_table] = n
            reaffectes += n
    audit.log(user.id, user.role, "reaffectation_organisation", entity, item_id,
              {"cible_id": cible, "reaffectes": reaffectes, "detail": detail})
    return {"reaffectes": reaffectes, "cible_id": cible, "detail": detail}
