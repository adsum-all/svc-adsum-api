"""Access groups (RBAC): grant platform access without touching member identity.

Everyone is a member with their own account (default role 'membre'). Platform
access is granted only by adding the member to an access group; the account role
is a derived cache of the member's groups (the highest role granted). Removing a
member from every group reverts the role to 'membre' WITHOUT deleting the
account, so a member never loses their own member-app login. Managed by admins.
"""
# ruff: noqa: E501
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .deps import require_roles
from .fields import ShortStr
from .schemas import UserMe
from .security import hash_password

router = APIRouter(prefix="/api/v1/admin", tags=["groupes"])

require_admin = require_roles("super_admin", "admin")

# Platform role hierarchy, to pick the highest role a member's groups grant.
_ROLE_RANK = {"membre": 0, "controleur": 1, "gestionnaire": 2, "direction": 3, "admin": 4, "super_admin": 5}


def _effective_role(membre_id: str, actor_role: str) -> str:
    """The highest platform role granted by the member's active groups, or 'membre'."""
    rows = db.fetch_all(
        "SELECT g.role_accorde FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = %s AND g.actif = true",
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
    temp = secrets.token_urlsafe(9)
    db.execute(
        "INSERT INTO utilisateur (email, hash_mdp, role, membre_id, actif, mdp_temporaire, doit_changer_mdp) "
        "VALUES (%s, %s, %s, %s, true, true, true) ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, membre_id = EXCLUDED.membre_id",
        (str(email), hash_password(temp), eff, membre_id),
        role=actor.role,
    )
    return eff, temp


class GroupeOut(BaseModel):
    id: str
    cle: str
    libelle: str
    description: str | None = None
    role_accorde: str
    systeme: bool = False
    actif: bool = True


class GroupeIn(BaseModel):
    cle: ShortStr
    libelle: ShortStr
    description: str | None = None
    role_accorde: ShortStr


class MembreGroupeIn(BaseModel):
    groupe_id: ShortStr


@router.get("/groupes", response_model=list[GroupeOut])
def list_groupes(user: Annotated[UserMe, Depends(require_admin)]) -> list[GroupeOut]:
    """The access-group catalogue (built-in and custom)."""
    rows = db.fetch_all(
        "SELECT id, cle, libelle, description, role_accorde, systeme, actif FROM groupe_acces WHERE actif = true ORDER BY systeme DESC, libelle ASC",
        (),
        role=user.role,
    )
    return [
        GroupeOut(
            id=str(r["id"]), cle=r["cle"], libelle=r["libelle"], description=r.get("description"),
            role_accorde=r["role_accorde"], systeme=bool(r["systeme"]), actif=bool(r["actif"]),
        )
        for r in rows
    ]


@router.post("/groupes", response_model=GroupeOut, status_code=status.HTTP_201_CREATED)
def create_groupe(payload: GroupeIn, user: Annotated[UserMe, Depends(require_roles("super_admin"))]) -> GroupeOut:
    """Create a custom access group (super_admin only). The granted role must be a
    known platform role."""
    if payload.role_accorde not in _ROLE_RANK:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_accorde invalide")
    created = db.execute(
        "INSERT INTO groupe_acces (cle, libelle, description, role_accorde, systeme) VALUES (%s, %s, %s, %s, false) "
        "ON CONFLICT (cle) DO NOTHING RETURNING id",
        (payload.cle.strip().lower(), payload.libelle, payload.description, payload.role_accorde),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="clé déjà utilisée")
    audit.log(user.id, user.role, "creation_groupe_acces", "groupe_acces", str(created["id"]), {"cle": payload.cle})
    return GroupeOut(id=str(created["id"]), cle=payload.cle.strip().lower(), libelle=payload.libelle, description=payload.description, role_accorde=payload.role_accorde, systeme=False, actif=True)


@router.get("/membres/{membre_id}/groupes")
def membre_groupes(membre_id: str, user: Annotated[UserMe, Depends(require_admin)]) -> dict[str, object]:
    """The groups a member belongs to and their resulting effective role."""
    rows = db.fetch_all(
        "SELECT g.id, g.cle, g.libelle, g.role_accorde FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = %s ORDER BY g.libelle ASC",
        (membre_id,),
        role=user.role,
    )
    return {
        "membre_id": membre_id,
        "effective_role": _effective_role(membre_id, user.role),
        "groupes": [{"id": str(r["id"]), "cle": r["cle"], "libelle": r["libelle"], "role_accorde": r["role_accorde"]} for r in rows],
    }


@router.post("/membres/{membre_id}/groupes")
def ajouter_au_groupe(membre_id: str, payload: MembreGroupeIn, user: Annotated[UserMe, Depends(require_admin)]) -> dict[str, object]:
    """Add a member to an access group, then sync their login account role.

    Grants platform access without changing who the member is: they keep their
    member account, now with the group's role. If they had no login yet, one is
    created on their member e-mail with a temporary password (returned once)."""
    if not db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
    if not db.fetch_one("SELECT id FROM groupe_acces WHERE id = %s AND actif = true", (payload.groupe_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe introuvable")
    db.execute(
        "INSERT INTO membre_groupe (membre_id, groupe_id, ajoute_par) VALUES (%s, %s, %s) ON CONFLICT (membre_id, groupe_id) DO NOTHING",
        (membre_id, payload.groupe_id, user.id),
        role=user.role,
    )
    eff, temp = _sync_account_role(membre_id, user)
    audit.log(user.id, user.role, "ajout_groupe_acces", "membre", membre_id, {"groupe_id": payload.groupe_id, "effective_role": eff})
    return {"membre_id": membre_id, "effective_role": eff, "mot_de_passe_temporaire": temp}


@router.delete("/membres/{membre_id}/groupes/{groupe_id}", status_code=status.HTTP_200_OK)
def retirer_du_groupe(membre_id: str, groupe_id: str, user: Annotated[UserMe, Depends(require_admin)]) -> dict[str, object]:
    """Remove a member from a group and re-sync their role. The account is never
    deleted: a member with no group falls back to 'membre' and keeps their login."""
    db.execute("DELETE FROM membre_groupe WHERE membre_id = %s AND groupe_id = %s", (membre_id, groupe_id), role=user.role)
    eff, _ = _sync_account_role(membre_id, user)
    audit.log(user.id, user.role, "retrait_groupe_acces", "membre", membre_id, {"groupe_id": groupe_id, "effective_role": eff})
    return {"membre_id": membre_id, "effective_role": eff}
