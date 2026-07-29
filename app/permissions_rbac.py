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

    Only GLOBAL memberships contribute here, exactly like ``_effective_role``: a
    scoped membership (coordination/intendance/commission/tribu) grants a bounded
    pilotage access through ``resolve_scope``/``require_perimetre``, never a global
    permission. Without this filter a scoped group would leak its whole role's
    permissions across the entire base, an escalation the pre-RBAC role gate refused.
    """
    perms: set[str] = set(permissions_du_role(user.role))
    if not user.membre_id:
        return frozenset(perms)
    rows = db.fetch_all(
        "SELECT g.mode, g.role_accorde, gp.permission, mg.application_code "
        "FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "LEFT JOIN groupe_permission gp ON gp.groupe_id = g.id AND g.mode = 'permissions' "
        "WHERE mg.membre_id = %s AND mg.actif = true AND g.actif = true AND mg.portee_type = 'global'",
        (user.membre_id,),
        role=user.role,
    )
    # A membership tagged with an application contributes only what that application
    # is allowed to exercise. Without this the tag bounded nothing: a right handed out
    # inside the collaboration workspace applied to the back office too, which is how
    # an Administration grant made from an application tab became platform-wide.
    etiquetees = {str(r["application_code"]) for r in rows if r.get("application_code")}
    autorisees_par_app = _permissions_par_application(etiquetees, user.role) if etiquetees else {}

    for r in rows:
        app = str(r["application_code"]) if r.get("application_code") else None
        permises = autorisees_par_app.get(app) if app else None
        if r["mode"] == "permissions":
            cle = r.get("permission")
            if cle and (permises is None or str(cle) in permises):
                perms.add(str(cle))
        else:
            accordees = set(permissions_du_role(str(r["role_accorde"])))
            perms |= accordees if permises is None else (accordees & permises)
    return frozenset(perms)


def _permissions_par_application(codes: set[str], role: str) -> dict[str, set[str]]:
    """What each named application is allowed to exercise, from the referential.

    Fails closed on purpose: an application absent from the referential grants
    nothing, rather than everything. A tag naming an application nobody has described
    yet is a configuration gap, and reading it as "no restriction" would reinstate
    exactly the hole this closes.

    The same answer covers the referential being unreachable, which is what happens
    if this code ships ahead of its migration. Letting the error travel would turn
    every permission check into a 500 for anybody holding a tagged membership, and a
    platform that answers 500 is worse than one that answers 403.
    """
    par_app: dict[str, set[str]] = {c: set() for c in codes}
    try:
        lignes = db.fetch_all(
            "SELECT application_code, permission FROM permission_application "
            "WHERE application_code = ANY(%s)",
            (list(codes),), role=role,
        )
    except Exception:  # noqa: BLE001 - referential not deployed yet: grant nothing
        return par_app
    for ligne in lignes:
        par_app.setdefault(str(ligne["application_code"]), set()).add(str(ligne["permission"]))
    return par_app


def require_permission(permission: str):
    """FastAPI dependency: 403 unless the account holds ``permission``."""

    def _dep(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
        if permission not in permissions_effectives(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission requise absente")
        return user

    return _dep


# The access-governance tables (membre_application_acces, membre_groupe, groupe_acces,
# groupe_permission, membre_application_role) are meant to be written only by an admin BASE
# role (super_admin/admin), matching the RLS write policies of migrations 0068/0134/0135. The
# intended RLS session role is the account's base role (utilisateur.role), not the
# effective/group-derived role.
ROLES_ECRITURE_ACCES = ("super_admin", "admin")


def assert_role_ecriture_acces(user: UserMe) -> None:
    """Reject a caller whose base role is not admin. This application-layer check is the
    EFFECTIVE enforcer of the admin-only rule: the API connects with the schema owner role,
    which is exempt from RLS on the tables declared ENABLE-only (membre_groupe, groupe_acces,
    groupe_permission, cf. 0068/0076) and, per ADR-0002, the backend/service role bypasses
    RLS generally. So without this check a non-admin base role holding a governance
    permission by delegation (a 'permissions' group) could actually write those tables. Do
    NOT relax this guard expecting RLS to catch the role: for the role dimension RLS is
    intent, not a runtime backstop. (The 0138 trigger IS a real backstop, but only for the
    visibility invariant, and runs even under the owner/BYPASSRLS connection.)"""
    if user.role not in ROLES_ECRITURE_ACCES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reserve aux administrateurs (super_admin, admin)",
        )


def require_permission_ecriture(permission: str):
    """FastAPI dependency for governance WRITE endpoints: holds ``permission`` AND has an
    admin base role, so the endpoint authority matches the RLS write policy of the
    access-governance tables it mutates. Depends directly on ``current_user`` (a module-level
    name so the stringized annotation resolves under ``from __future__ import annotations``)
    and performs both checks inline."""

    def _dep(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
        if permission not in permissions_effectives(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission requise absente")
        assert_role_ecriture_acces(user)
        return user

    return _dep
