"""Access groups (RBAC): grant platform access without touching member identity.

Everyone is a member with their own account (default role 'membre'). Platform
access is granted only by adding the member to an access group; the account role
is a derived cache of the member's groups (the highest role granted). Removing a
member from every group reverts the role to 'membre' WITHOUT deleting the
account, so a member never loses their own member-app login. Managed by admins.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, db
from .groupes_modeles import (
    GroupeIn,
    GroupeOut,
    MembreGroupeIn,
    UpdateGroupeIn,
)
from .groupes_modeles import (
    champs_documentation as _champs_documentation,
)
from .groupes_roles import (
    _GLOBAL_ONLY_ROLES,
    _PORTEE_TABLES,
    _ROLE_RANK,
    _assert_peut_gerer,
    _assert_super_admin_preserve,
    _effective_role,
    _sync_account_role,
    resync_membres_du_groupe,
)
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["groupes"])



def _valider_application_code(code: str | None, role: str) -> str | None:
    """A group's application tag must name a real catalogue application (or be None)."""
    if code is None:
        return None
    code = code.strip().lower()
    if not code:
        return None
    if not db.fetch_one("SELECT code FROM application WHERE code = %s", (code,), role=role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="application inconnue")
    return code


def _permissions_du_groupe(groupe_id: str, role: str) -> list[str]:
    """The atomic permissions a 'permissions' group grants (sorted, empty otherwise)."""
    rows = db.fetch_all(
        "SELECT permission FROM groupe_permission WHERE groupe_id = %s ORDER BY permission",
        (groupe_id,),
        role=role,
    )
    return [str(r["permission"]) for r in rows]


def _assert_peut_accorder_permissions(perms: list[str], actor: UserMe) -> None:
    """Guard the permission set of a 'permissions' group (least privilege, F2).

    super_admin may grant anything. Anyone else may only put in the group a
    permission they themselves hold, and never a 'critique' permission: this stops
    an admin from minting a group that exceeds their own reach or hands out system
    powers. Unknown keys are rejected so a typo never becomes a silent no-op.
    """
    from .permissions_data import CATALOGUE
    from .permissions_rbac import permissions_effectives

    inconnues = [p for p in perms if p not in CATALOGUE]
    if inconnues:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"permission(s) inconnue(s): {', '.join(inconnues)}")
    if actor.role == "super_admin":
        return
    held = permissions_effectives(actor)
    for p in perms:
        if CATALOGUE[p].get("risque") == "critique":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"seule la super-administration peut accorder la permission critique {p}")
        if p not in held:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"vous ne pouvez pas accorder une permission que vous ne détenez pas: {p}")


def _valider_portee(role_accorde: str, portee_type: str, portee_id: str | None, actor_role: str) -> None:
    """Check the requested perimeter is coherent with the group and exists.

    A global-only role (super_admin / admin) can only be granted globally. A
    scopable role may be granted globally or on a real coordination / intendance /
    commission / tribu unit, whose existence is verified.
    """
    if portee_type == "global":
        if portee_id is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="une portée globale ne prend pas d'unité")
        return
    if role_accorde in _GLOBAL_ONLY_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="ce groupe ne peut être accordé que globalement")
    table = _PORTEE_TABLES.get(portee_type)
    if not table:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type de portée invalide")
    if not portee_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unité de portée requise")
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (portee_id,), role=actor_role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unité de portée introuvable")


def _groupe_out(r: dict[str, object], role: str) -> GroupeOut:
    gid = str(r["id"])
    mode = str(r.get("mode") or "role")
    return GroupeOut(
        id=gid, cle=r["cle"], libelle=r["libelle"], description=r.get("description"),
        role_accorde=r["role_accorde"], mode=mode,
        permissions=_permissions_du_groupe(gid, role) if mode == "permissions" else [],
        membres_count=int(r.get("membres_count") or 0),
        systeme=bool(r["systeme"]), actif=bool(r["actif"]),
        application_code=r.get("application_code"),
    )


@router.get("/groupes", response_model=list[GroupeOut])
def list_groupes(
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))],
    inclure_inactifs: bool = False,
) -> list[GroupeOut]:
    """The access-group catalogue (built-in and custom), with mode, permissions and
    member count so the admin can browse and manage without extra round-trips.

    Inactive groups are hidden by default (assignment picker) and shown on demand
    (management view), so a deactivated group can be reviewed and reactivated.
    """
    where = "" if inclure_inactifs else "WHERE g.actif = true"
    rows = db.fetch_all(
        "SELECT g.id, g.cle, g.libelle, g.description, g.role_accorde, g.mode, g.systeme, g.actif, "
        "g.application_code, "
        "(SELECT count(*) FROM membre_groupe mg WHERE mg.groupe_id = g.id AND mg.actif = true) AS membres_count "
        f"FROM groupe_acces g {where} ORDER BY g.systeme DESC, g.libelle ASC",
        (),
        role=user.role,
    )
    return [_groupe_out(r, user.role) for r in rows]


def _remplacer_permissions_groupe(groupe_id: str, perms: list[str], role: str) -> None:
    """Set a 'permissions' group's granted set to exactly ``perms`` (idempotent)."""
    db.execute("DELETE FROM groupe_permission WHERE groupe_id = %s", (groupe_id,), role=role)
    for p in sorted(set(perms)):
        db.execute(
            "INSERT INTO groupe_permission (groupe_id, permission) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (groupe_id, p),
            role=role,
        )


@router.post("/groupes", response_model=GroupeOut, status_code=status.HTTP_201_CREATED)
def create_groupe(payload: GroupeIn, user: Annotated[UserMe, Depends(require_permission_ecriture("acces.systeme"))]) -> GroupeOut:
    """Create a custom access group.

    Two modes. A 'role' group grants a known platform role (an admin cannot create
    a group above their own rank). A 'permissions' group grants an explicit set of
    atomic permissions, keeps role_accorde='membre', and is bound by the least
    privilege guard (no critical permission, nothing the actor does not hold).
    """
    mode = payload.mode if payload.mode in ("role", "permissions") else "role"
    if mode == "permissions":
        if not payload.permissions:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un groupe de permissions doit accorder au moins une permission")
        _assert_peut_accorder_permissions(payload.permissions, user)
        role_accorde = "membre"
    else:
        role_accorde = payload.role_accorde or ""
        if role_accorde not in _ROLE_RANK:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_accorde invalide")
        # An admin cannot mint a group that grants a role above their own rank.
        _assert_peut_gerer(user, role_accorde, "")
    application_code = _valider_application_code(payload.application_code, user.role)
    doc = _champs_documentation(payload)
    # The group and its permissions are written in ONE transaction: writing them
    # apart would leave a permission group with no permission behind as soon as a
    # single key is refused by the referential.
    colonnes = ["cle", "libelle", "description", "role_accorde", "systeme", "mode", "application_code", "cree_par", *doc]
    valeurs = [payload.cle.strip().lower(), payload.libelle, payload.description, role_accorde,
               False, mode, application_code, user.id, *doc.values()]
    with db.connection(user.role) as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO groupe_acces ({', '.join(colonnes)}) VALUES ({', '.join(['%s'] * len(colonnes))}) "
            "ON CONFLICT (cle) DO NOTHING RETURNING id",
            valeurs,
        )
        created = cur.fetchone()
        if not created:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="clé déjà utilisée")
        gid = str(created["id"])
        if mode == "permissions":
            paires = sorted(set(payload.permissions))
            plat: list[object] = []
            for p in paires:
                plat.extend([gid, p])
            cur.execute(
                "INSERT INTO groupe_permission (groupe_id, permission) VALUES "
                + ", ".join(["(%s, %s)"] * len(paires)) + " ON CONFLICT DO NOTHING",
                plat,
            )
    audit.log(user.id, user.role, "creation_groupe_acces", "groupe_acces", gid, {"cle": payload.cle, "mode": mode, "role_accorde": role_accorde, "application_code": application_code, "permissions": payload.permissions if mode == "permissions" else None, "documentation": list(doc)})
    return GroupeOut(id=gid, cle=payload.cle.strip().lower(), libelle=payload.libelle, description=payload.description, role_accorde=role_accorde, mode=mode, permissions=sorted(set(payload.permissions)) if mode == "permissions" else [], membres_count=0, systeme=False, actif=True, application_code=application_code)


@router.patch("/groupes/{groupe_id}", response_model=GroupeOut)
def update_groupe(groupe_id: str, payload: UpdateGroupeIn, user: Annotated[UserMe, Depends(require_permission_ecriture("acces.systeme"))]) -> GroupeOut:
    """Edit a custom group: label, description, active state, and for a permissions
    group its granted set. System groups are read-only (their behaviour is relied
    upon platform-wide); attempting to edit one is refused."""
    g = db.fetch_one("SELECT id, cle, libelle, description, role_accorde, mode, systeme, actif FROM groupe_acces WHERE id = %s", (groupe_id,), role=user.role)
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe introuvable")
    if bool(g["systeme"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="un groupe système ne peut pas être modifié")
    # EVERYTHING is validated before ANYTHING is written. The previous order applied
    # the label, the active state and the role resynchronisation first, then checked
    # the permission set: a refused permission left the group already deactivated,
    # its members already stripped of their rights, and no audit line at all, since
    # the exception escaped before the journal was written.
    if payload.permissions is not None:
        if str(g["mode"]) != "permissions":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="seul un groupe de permissions porte des permissions")
        if not payload.permissions:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un groupe de permissions doit accorder au moins une permission")
        _assert_peut_accorder_permissions(payload.permissions, user)

    fields: dict[str, object] = {}
    if payload.libelle is not None:
        fields["libelle"] = payload.libelle
    if payload.description is not None:
        fields["description"] = payload.description
    if payload.actif is not None:
        fields["actif"] = payload.actif
    # exclude_unset so sending null explicitly DETACHES the group from its application,
    # while omitting the field leaves the tag untouched.
    if "application_code" in payload.model_fields_set:
        fields["application_code"] = _valider_application_code(payload.application_code, user.role)
    fields.update(_champs_documentation(payload))
    if fields:
        fields["maj_le"] = "now()"
        fields["maj_par"] = user.id

    # One transaction for the whole change: the group's own columns and its permission
    # set never land half applied.
    with db.connection(user.role) as conn, conn.cursor() as cur:
        if fields:
            assignations = ", ".join("maj_le = now()" if k == "maj_le" else f"{k} = %s" for k in fields)
            params = [v for k, v in fields.items() if k != "maj_le"]
            cur.execute(f"UPDATE groupe_acces SET {assignations} WHERE id = %s", (*params, groupe_id))
        if payload.permissions is not None:
            cur.execute("DELETE FROM groupe_permission WHERE groupe_id = %s", (groupe_id,))
            voulues = sorted(set(payload.permissions))
            plat: list[object] = []
            for p in voulues:
                plat.extend([groupe_id, p])
            cur.execute(
                "INSERT INTO groupe_permission (groupe_id, permission) VALUES "
                + ", ".join(["(%s, %s)"] * len(voulues)) + " ON CONFLICT DO NOTHING",
                plat,
            )

    # Deactivating (or reactivating) a group changes what its members are entitled to,
    # and the enforcement path starts from the CACHED utilisateur.role. Resync every
    # member of the group so a deactivated Administration group really withdraws the
    # admin permissions instead of leaving a stale elevated cache behind (and the
    # review surfaces then tell the truth). Done after the write, never before.
    if payload.actif is not None and payload.actif != bool(g["actif"]):
        resync_membres_du_groupe(groupe_id, user)
    audit.log(user.id, user.role, "modification_groupe_acces", "groupe_acces", groupe_id,
              {"champs": [k for k in fields if k not in ("maj_le", "maj_par")], "permissions": payload.permissions})
    row = db.fetch_one(
        "SELECT g.id, g.cle, g.libelle, g.description, g.role_accorde, g.mode, g.systeme, g.actif, "
        "g.application_code, "
        "(SELECT count(*) FROM membre_groupe mg WHERE mg.groupe_id = g.id AND mg.actif = true) AS membres_count "
        "FROM groupe_acces g WHERE g.id = %s",
        (groupe_id,),
        role=user.role,
    )
    return _groupe_out(row, user.role)


@router.delete("/groupes/{groupe_id}", status_code=status.HTTP_200_OK)
def delete_groupe(groupe_id: str, user: Annotated[UserMe, Depends(require_permission_ecriture("acces.systeme"))]) -> dict[str, object]:
    """Delete a custom group. A system group is never deleted; a group that still
    has members is refused (409) so nobody silently loses access: remove the
    members first. Deletion is audited."""
    g = db.fetch_one("SELECT id, cle, systeme FROM groupe_acces WHERE id = %s", (groupe_id,), role=user.role)
    if not g:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe introuvable")
    if bool(g["systeme"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="un groupe système ne peut pas être supprimé")
    n = db.fetch_one("SELECT count(*) AS n FROM membre_groupe WHERE groupe_id = %s AND actif = true", (groupe_id,), role=user.role)
    if int((n or {}).get("n", 0)) > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="retirez d'abord les membres de ce groupe avant de le supprimer")
    db.execute("DELETE FROM groupe_permission WHERE groupe_id = %s", (groupe_id,), role=user.role)
    db.execute("DELETE FROM groupe_acces WHERE id = %s", (groupe_id,), role=user.role)
    audit.log(user.id, user.role, "suppression_groupe_acces", "groupe_acces", groupe_id, {"cle": g["cle"]})
    return {"supprime": True, "id": groupe_id}


@router.get("/membres/{membre_id}/groupes")
def membre_groupes(membre_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, object]:
    """The scoped group memberships of a member, with who granted each and when, and the effective global role."""
    rows = db.fetch_all(
        "SELECT mg.id AS appartenance_id, g.id AS groupe_id, g.cle, g.libelle, g.role_accorde, "
        "g.actif AS groupe_actif, g.mode, "
        "mg.portee_type, mg.portee_id, mg.ajoute_le, "
        "COALESCE(pc.nom, pin.nom, pk.nom, pt.nom) AS portee_libelle, "
        "COALESCE(NULLIF(trim(coalesce(am.prenoms, '') || ' ' || coalesce(am.nom, '')), ''), ua.email) AS ajoute_par_nom "
        "FROM membre_groupe mg "
        "JOIN groupe_acces g ON g.id = mg.groupe_id "
        "LEFT JOIN coordination pc ON mg.portee_type = 'coordination' AND pc.id = mg.portee_id "
        "LEFT JOIN intendance pin ON mg.portee_type = 'intendance' AND pin.id = mg.portee_id "
        "LEFT JOIN commission pk ON mg.portee_type = 'commission' AND pk.id = mg.portee_id "
        "LEFT JOIN tribu pt ON mg.portee_type = 'tribu' AND pt.id = mg.portee_id "
        "LEFT JOIN utilisateur ua ON ua.id = mg.ajoute_par "
        "LEFT JOIN membre am ON am.id = ua.membre_id "
        "WHERE mg.membre_id = %s AND mg.actif = true ORDER BY g.libelle ASC, mg.portee_type ASC",
        (membre_id,),
        role=user.role,
    )
    return {
        "membre_id": membre_id,
        "effective_role": _effective_role(membre_id, user.role),
        "groupes": [
            {
                "appartenance_id": str(r["appartenance_id"]),
                "groupe_id": str(r["groupe_id"]),
                "cle": r["cle"],
                "libelle": r["libelle"],
                "role_accorde": r["role_accorde"],
                # Surface the group's own state so a review never shows a deactivated
                # group as if it still granted its role.
                "groupe_actif": bool(r.get("groupe_actif")),
                "mode": str(r.get("mode") or "role"),
                "portee_type": r["portee_type"],
                "portee_id": str(r["portee_id"]) if r.get("portee_id") else None,
                "portee_libelle": r.get("portee_libelle"),
                "ajoute_le": r["ajoute_le"].isoformat() if r.get("ajoute_le") else None,
                "ajoute_par_nom": r.get("ajoute_par_nom"),
            }
            for r in rows
        ],
    }


@router.post("/membres/{membre_id}/groupes")
def ajouter_au_groupe(membre_id: str, payload: MembreGroupeIn, user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))]) -> dict[str, object]:
    """Add a member to an access group, on a global or scoped perimeter.

    A global membership elevates the account's back-office role; a scoped one
    grants a bounded pilotage access to a single unit without any global role. A
    member may hold several scoped memberships (the same group over several
    perimeters). The identity is never changed; a first grant creates the login
    on the member's e-mail with a temporary password (returned once)."""
    if not db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
    groupe = db.fetch_one("SELECT id, role_accorde, mode FROM groupe_acces WHERE id = %s AND actif = true", (payload.groupe_id,), role=user.role)
    if not groupe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe introuvable")
    _assert_peut_gerer(user, str(groupe["role_accorde"]), membre_id)
    # A 'permissions' group grants global permissions (permissions_effectives only
    # counts global memberships); a scoped grant would be a silent no-op, so refuse it.
    if str(groupe.get("mode") or "role") == "permissions":
        if payload.portee_type != "global":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un groupe de permissions ne s'accorde qu'en portée globale")
        # Least privilege on ASSIGNMENT too, not only on creation: an admin cannot
        # hand out a permission group that carries a critical permission or one they
        # do not hold themselves, closing the puppet-account escalation path.
        _assert_peut_accorder_permissions(_permissions_du_groupe(str(groupe["id"]), user.role), user)
    _valider_portee(str(groupe["role_accorde"]), payload.portee_type, payload.portee_id, user.role)
    # A membership is closed rather than erased, so the row of a previous stay is
    # still there and the unique index would swallow a plain insert: the member would
    # be "added" without ever regaining access. Reopen it first, and only insert when
    # there is nothing to reopen.
    rouvert = db.execute(
        "UPDATE membre_groupe SET actif = true, retire_le = NULL, retire_par = NULL, "
        "ajoute_par = %s, ajoute_le = now() "
        "WHERE membre_id = %s AND groupe_id = %s AND portee_type = %s "
        "AND portee_id IS NOT DISTINCT FROM %s AND actif = false RETURNING id",
        (user.id, membre_id, payload.groupe_id, payload.portee_type, payload.portee_id),
        role=user.role,
    )
    if not rouvert:
        db.execute(
            "INSERT INTO membre_groupe (membre_id, groupe_id, portee_type, portee_id, ajoute_par) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (membre_id, payload.groupe_id, payload.portee_type, payload.portee_id, user.id),
            role=user.role,
        )
    eff, temp = _sync_account_role(membre_id, user)
    audit.log(
        user.id, user.role, "ajout_groupe_acces", "membre", membre_id,
        {"groupe_id": payload.groupe_id, "portee_type": payload.portee_type, "portee_id": payload.portee_id, "effective_role": eff},
    )
    return {"membre_id": membre_id, "effective_role": eff, "mot_de_passe_temporaire": temp}


@router.delete("/membres/{membre_id}/groupes/{appartenance_id}", status_code=status.HTTP_200_OK)
def retirer_du_groupe(membre_id: str, appartenance_id: str, user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))]) -> dict[str, object]:
    """Remove ONE membership (a group on a given perimeter) and re-sync the role.

    Targets a single ``membre_groupe`` row so multi-membership is respected. The
    account is never deleted: a member with no membership left falls back to
    'membre' and keeps their own login."""
    row = db.fetch_one(
        "SELECT mg.id, mg.groupe_id, g.cle, g.role_accorde FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.id = %s AND mg.membre_id = %s AND mg.actif = true",
        (appartenance_id, membre_id),
        role=user.role,
    )
    groupe_id = str(row["groupe_id"]) if row else None
    if row:
        # Same hierarchy guard as granting: an admin cannot demote a super_admin by
        # pulling them out of the super_administration group (F1 reverse path).
        _assert_peut_gerer(user, str(row["role_accorde"]), membre_id)
        _assert_super_admin_preserve(membre_id, str(row["role_accorde"]), user)
        # Closed rather than erased: the membership keeps a dated trace of who was in
        # the group and until when. Every read path filters on mg.actif, so the access
        # itself is revoked as surely as by a delete.
        db.execute(
            "UPDATE membre_groupe SET actif = false, retire_le = now(), retire_par = %s "
            "WHERE id = %s AND membre_id = %s",
            (user.id, appartenance_id, membre_id), role=user.role,
        )
    eff, _ = _sync_account_role(membre_id, user)
    # The group id travels with the event so the group sheet can show its own history
    # of arrivals and departures, which an appartenance_id alone could not resolve.
    audit.log(user.id, user.role, "retrait_groupe_acces", "membre", membre_id,
              {"appartenance_id": appartenance_id, "groupe_id": groupe_id,
               "groupe_cle": row["cle"] if row else None, "effective_role": eff})
    return {"membre_id": membre_id, "effective_role": eff}
