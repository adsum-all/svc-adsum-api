"""Permission resolution and FastAPI enforcement (executory RBAC).

Deny-by-default: a permission not held is refused. The pure data lives in
app.permissions_data; this module adds the DB-backed resolution and the
require_permission dependency.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from . import db
from .auth import current_user
from .permissions_data import permissions_du_role
from .schemas import UserMe


def permissions_effectives(user: UserMe) -> frozenset[str]:
    """Effective permission set: role-cache compat plus active group memberships.

    A 'role' group resolves via role_permission[role_accorde]; a 'permissions' group
    via groupe_permission. Union, deny-by-default.
    """
    perms: set[str] = set(permissions_du_role(user.role))
    if not user.membre_id:
        return frozenset(perms)
    rows = db.fetch_all(
        "SELECT g.mode, g.role_accorde, gp.permission "
        "FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "LEFT JOIN groupe_permission gp ON gp.groupe_id = g.id AND g.mode = 'permissions' "
        "WHERE mg.membre_id = %s AND g.actif = true",
        (user.membre_id,),
        role=user.role,
    )
    for r in rows:
        if r["mode"] == "permissions":
            if r.get("permission"):
                perms.add(str(r["permission"]))
        else:
            perms |= set(permissions_du_role(str(r["role_accorde"])))
    return frozenset(perms)


def require_permission(permission: str):
    """FastAPI dependency: 403 unless the account holds ``permission``."""

    def _dep(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
        if permission not in permissions_effectives(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission requise absente")
        return user

    return _dep
