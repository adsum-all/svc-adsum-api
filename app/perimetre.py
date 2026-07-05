"""Perimeter (scope) resolution for the responsable-de-perimetre pilotage layer.

The security spine of the pilotage mission: a member who holds an active,
confirmed responsable function (coordinateur, intendant, patriarche, and later
responsable_pays / responsable_continental) gains a read/pilot access strictly
bounded to their own perimeter, WITHOUT any back-office or account privilege.

The scope is DERIVED, never stored: it is computed from the responsible's own
attachment (``membre.coordination_id`` / ``intendance_id`` / ``tribu_id``) and
the descending closure of the self-hierarchies (``parent_id``). It is applied as
a SQL predicate on every scoped query, so a France coordination can never read a
USA coordination, and one intendance can never read another. Global roles
(super_admin, admin, direction) get an unbounded oversight scope.

This module adds no table and no migration: the closure is computed on the fly
with a recursive CTE over the existing organisation tables.
"""
# ruff: noqa: E501
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status

from . import db
from .auth import current_user
from .schemas import UserMe
from .scope import Scope

# Roles with unbounded oversight (they already govern the whole base).
GLOBAL_ROLES = frozenset({"super_admin", "admin", "direction"})

# Functions that confer a bounded pilotage scope, mapped to the attachment axis
# the scope is derived from on the responsible's own member record.
_AXIS_BY_FONCTION = {
    "coordinateur": "coordination",
    "intendant": "intendance",
    "patriarche": "tribu",
}
# Attribute-based functions: the perimeter value lives in membre_fonction.perimetre.
_PAYS_FONCTION = "responsable_pays"
_CONTINENT_FONCTION = "responsable_continental"


def _closure(table: str, roots: set[str], role: str) -> frozenset[str]:
    """Descending closure of a set of roots over ``table.parent_id`` (self-FK)."""
    if not roots:
        return frozenset()
    rows = db.fetch_all(
        f"WITH RECURSIVE d AS ("
        f"  SELECT id FROM {table} WHERE id = ANY(%s::uuid[]) "
        f"  UNION SELECT t.id FROM {table} t JOIN d ON t.parent_id = d.id"
        f") SELECT id FROM d",
        (list(roots),),
        role=role,
    )
    return frozenset(str(r["id"]) for r in rows)


def resolve_scope(user: UserMe) -> Scope:
    """Compute the pilotage scope of the authenticated user.

    Global roles get an unbounded scope. Otherwise the scope is derived from the
    member's active, confirmed responsable functions and their attachment, with
    the descending hierarchy closure applied.
    """
    if user.role in GLOBAL_ROLES:
        return Scope(is_global=True, coordination_ids=frozenset(), intendance_ids=frozenset(), tribu_ids=frozenset())
    if not user.membre_id:
        return Scope(False, frozenset(), frozenset(), frozenset())

    fonctions = db.fetch_all(
        "SELECT lower(fonction_cle) AS cle, perimetre FROM membre_fonction "
        "WHERE membre_id = %s AND actif = true AND confirmee = true",
        (user.membre_id,),
        role=user.role,
    )
    cles = {str(f["cle"]) for f in fonctions}
    has_perimeter_fn = bool(cles & (set(_AXIS_BY_FONCTION) | {_PAYS_FONCTION, _CONTINENT_FONCTION}))

    # A member is responsable of a commission through the existing model column
    # commission.responsable_id -> membre.id (not invented, migration 0003).
    commissions = db.fetch_all(
        "SELECT id FROM commission WHERE responsable_id = %s", (user.membre_id,), role=user.role
    )
    commission_resp = frozenset(str(r["id"]) for r in commissions)

    if not has_perimeter_fn and not commission_resp:
        return Scope(False, frozenset(), frozenset(), frozenset())

    me = db.fetch_one(
        "SELECT coordination_id, intendance_id, tribu_id FROM membre WHERE id = %s",
        (user.membre_id,),
        role=user.role,
    ) or {}

    coord_roots: set[str] = set()
    intend_roots: set[str] = set()
    tribu_roots: set[str] = set()
    commission_ids: frozenset[str] = frozenset()

    if has_perimeter_fn:
        # Perimeter responsable (coordinateur / intendant / patriarche / pays /
        # continent): governs the whole perimeter, no commission restriction.
        if "coordinateur" in cles and me.get("coordination_id"):
            coord_roots.add(str(me["coordination_id"]))
        if "intendant" in cles and me.get("intendance_id"):
            intend_roots.add(str(me["intendance_id"]))
        if "patriarche" in cles and me.get("tribu_id"):
            tribu_roots.add(str(me["tribu_id"]))
        pays = {str(f["perimetre"]) for f in fonctions if str(f["cle"]) == _PAYS_FONCTION and f.get("perimetre")}
        continents = {str(f["perimetre"]) for f in fonctions if str(f["cle"]) == _CONTINENT_FONCTION and f.get("perimetre")}
        for column, values in (("pays_code", pays), ("continent", continents)):
            if not values:
                continue
            for table, roots in (("coordination", coord_roots), ("intendance", intend_roots)):
                found = db.fetch_all(
                    f"SELECT id FROM {table} WHERE {column} = ANY(%s)",
                    (list(values),),
                    role=user.role,
                )
                roots.update(str(r["id"]) for r in found)
    else:
        # Pure commission responsable: scope = their OWN perimeter (coordination or
        # intendance they belong to) INTERSECTED with the commission(s) they lead.
        # They never see the same commission in another perimeter.
        if me.get("coordination_id"):
            coord_roots.add(str(me["coordination_id"]))
        if me.get("intendance_id"):
            intend_roots.add(str(me["intendance_id"]))
        commission_ids = commission_resp

    return Scope(
        is_global=False,
        coordination_ids=_closure("coordination", coord_roots, user.role),
        intendance_ids=_closure("intendance", intend_roots, user.role),
        tribu_ids=frozenset(tribu_roots),
        commission_ids=commission_ids,
    )


@dataclass(frozen=True)
class PerimetreContext:
    """The authenticated responsable and their resolved scope."""

    user: UserMe
    scope: Scope


def require_perimetre(user: Annotated[UserMe, Depends(current_user)]) -> PerimetreContext:
    """Dependency granting access to the pilotage layer for a responsable.

    Allows global roles and any member holding a confirmed responsable function
    with a non-empty scope; refuses everyone else with 403. Removing the function
    (or its confirmation) empties the scope and revokes access immediately.
    """
    scope = resolve_scope(user)
    if scope.is_empty():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="acces reserve aux responsables de perimetre",
        )
    return PerimetreContext(user=user, scope=scope)
