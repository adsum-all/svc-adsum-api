"""Platform role derived from a member's access groups, and its safety rails.

Split out of ``groupes.py`` to keep it within the size the project allows. The rules
enforced here are the ones that decide how far an account reaches: which role the
groups actually grant, the refusal to lose the last super administrator, and the ban
on granting oneself a rank one does not already hold.
"""
# ruff: noqa: E501
from __future__ import annotations

import secrets

from fastapi import HTTPException, status

from . import db
from .schemas import UserMe
from .security import hash_password

# Platform role hierarchy, to pick the highest role a member's groups grant.
_ROLE_RANK = {"membre": 0, "controleur": 1, "gestionnaire": 2, "direction": 3, "admin": 4, "super_admin": 5}

# Roles that only make sense globally: they govern the whole base, so a group
# granting them can never be scoped to a single organisational unit.
_GLOBAL_ONLY_ROLES = frozenset({"super_admin", "admin"})

# Scopable perimeter types and the organisation table each ``portee_id`` points to.
_PORTEE_TABLES = {
    "coordination": "coordination",
    "intendance": "intendance",
    "commission": "commission",
    "tribu": "tribu",
}


def _super_admins_actifs(role: str) -> int:
    """Count active super_admin login accounts (the availability floor)."""
    r = db.fetch_one("SELECT count(*) AS n FROM utilisateur WHERE role = 'super_admin' AND actif = true", (), role=role)
    return int((r or {}).get("n", 0))


def _assert_super_admin_preserve(membre_id: str, role_accorde: str, actor: UserMe) -> None:
    """Never let the system lose its last super_admin, nor let one self-demote.

    Removing a super_administration membership is refused when it would drop this
    member from super_admin AND either the actor is removing their own access, or
    this is the last active super_admin account (availability floor, M1).
    """
    if role_accorde != "super_admin":
        return
    # Does the member keep super_admin via another active super_admin group?
    autres = db.fetch_one(
        "SELECT count(*) AS n FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = %s AND mg.actif = true AND g.actif = true AND g.role_accorde = 'super_admin'",
        (membre_id,),
        role=actor.role,
    )
    if int((autres or {}).get("n", 0)) > 1:
        return  # they stay super_admin through another group
    if actor.membre_id and actor.membre_id == membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="vous ne pouvez pas retirer votre propre accès super-administration")
    if _super_admins_actifs(actor.role) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="impossible de retirer le dernier super-administrateur actif")


def _assert_peut_gerer(actor: UserMe, role_accorde: str, membre_cible_id: str) -> None:
    """Guard against privilege escalation when granting or revoking a group.

    Rules (deny-by-default):
    - A super_admin may manage any group (including the ones granting super_admin).
    - Anyone else may only manage a group whose granted role is STRICTLY below
      their own rank; in particular no admin can touch a group granting admin or
      super_admin, closing the self-promotion path.
    - No one below super_admin may manage their own access (no self-elevation).
    """
    if actor.role != "super_admin":
        if actor.membre_id and membre_cible_id == actor.membre_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="vous ne pouvez pas modifier vos propres accès")
        if _ROLE_RANK.get(actor.role, 0) <= _ROLE_RANK.get(role_accorde, 99):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="vous ne pouvez pas gérer un groupe accordant un rôle égal ou supérieur au vôtre")


def _effective_role(membre_id: str, actor_role: str) -> str:
    """The highest GLOBAL platform role granted by the member's active groups, or 'membre'.

    Only GLOBAL memberships elevate the account role (and thus back-office reach).
    A scoped membership (coordination/intendance/commission/tribu) grants a bounded
    pilotage access through :func:`app.perimetre.resolve_scope`, never a global
    back-office role, so the account role stays 'membre' and no data leaks outside
    the perimeter.
    """
    rows = db.fetch_all(
        "SELECT g.role_accorde FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = %s AND mg.actif = true AND g.actif = true AND mg.portee_type = 'global'",
        (membre_id,),
        role=actor_role,
    )
    roles = [str(r["role_accorde"]) for r in rows]
    if not roles:
        return "membre"
    return max(roles, key=lambda r: _ROLE_RANK.get(r, 0))


def _sync_account_role(membre_id: str, actor: UserMe) -> tuple[str, str | None]:
    """Recompute the member's role from their groups and sync the login account.

    Returns (effective_role, temp_password). A member who gains platform access
    but has no login account yet gets one created on their member e-mail with a
    temporary password (returned once). The account is never deleted.
    """
    eff = _effective_role(membre_id, actor.role)
    existing = db.fetch_one("SELECT id FROM utilisateur WHERE membre_id = %s", (membre_id,), role=actor.role)
    if existing:
        db.execute("UPDATE utilisateur SET role = %s WHERE membre_id = %s", (eff, membre_id), role=actor.role)
        return eff, None
    # No login account yet: create one on the member's own e-mail so the person
    # keeps a single account. Only needed the first time access is granted.
    membre = db.fetch_one("SELECT email FROM membre WHERE id = %s", (membre_id,), role=actor.role)
    email = (membre or {}).get("email")
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="le membre n'a pas d'e-mail pour créer un accès")
    # Never silently re-point an existing account bound to a different member:
    # only adopt an account whose e-mail is free or already this member's, else
    # refuse (F2: no account hijack, no false audit attribution).
    par_email = db.fetch_one("SELECT id, membre_id FROM utilisateur WHERE email = %s", (str(email),), role=actor.role)
    if par_email:
        if par_email.get("membre_id") not in (None, membre_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="un compte existe déjà pour cet e-mail, rattaché à un autre membre")
        db.execute(
            "UPDATE utilisateur SET role = %s, membre_id = %s WHERE id = %s",
            (eff, membre_id, str(par_email["id"])),
            role=actor.role,
        )
        return eff, None
    # No account at all: create one with a temporary password that expires, like
    # the inscription path (F4: no non-expiring temporary credential).
    temp = secrets.token_urlsafe(9)
    db.execute(
        "INSERT INTO utilisateur (email, hash_mdp, role, membre_id, actif, mdp_temporaire, mdp_expire_le, doit_changer_mdp) "
        "VALUES (%s, %s, %s, %s, true, true, now() + interval '7 days', true)",
        (str(email), hash_password(temp), eff, membre_id),
        role=actor.role,
    )
    return eff, temp


def resync_membres_du_groupe(groupe_id: str, actor: UserMe) -> None:
    """Recompute the cached account role of every member of a group. Called when the
    group's active state flips, so entitlements never outlive the group state."""
    membres = db.fetch_all(
        "SELECT DISTINCT membre_id FROM membre_groupe WHERE groupe_id = %s AND actif = true",
        (groupe_id,),
        role=actor.role,
    )
    for m in membres:
        _sync_account_role(str(m["membre_id"]), actor)
