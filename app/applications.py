# ruff: noqa: E501 - SQL upserts and guards carry long literals
"""« Mes applications » and the back-office governance of per-member application access.

LEVEL 1 of authorisation: whether a member can SEE and OPEN an application. This is
distinct from the RBAC roles (LEVEL 2, what they can do) and the perimeter scoping
(LEVEL 3, which data). No access is implicit: a member sees an application only when an
active grant exists in ``membre_application_acces``. The member self-endpoint returns
only their own active grants; the back-office endpoints (reserved to ``acces.administrer``)
grant or revoke access, always audited. The technical super-admin bypass is deliberately
NOT applied here: this surface is about ordinary members, never the emergency identity.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .auth import current_user
from .groupes_roles import _assert_peut_gerer
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["applications"])


class ApplicationOut(BaseModel):
    code: str
    nom: str
    description: str | None = None
    url: str | None = None
    actif: bool = True
    # For the back-office member view: whether THIS member currently has access.
    acces_actif: bool = False
    # True for applications every member sees by default (the member space), where
    # visibility is automatic and cannot be granted or revoked per member.
    est_defaut: bool = False
    # For the member view: whether the member can actually CONNECT (default app, or
    # visibility PLUS at least one access group tied to the application). Visibility
    # alone shows the card but the gate refuses the connection.
    ouvrable: bool = True


@router.get("/membres/me/applications", response_model=list[ApplicationOut])
def mes_applications(user: Annotated[UserMe, Depends(current_user)]) -> list[ApplicationOut]:
    """The applications the connected member has an ACTIVE grant to (LEVEL 1). Returns an
    empty list when the member has no additional application, never a locked/greyed one."""
    if not user.membre_id:
        # A non-member account (e.g. a technical identity) has no « Mes applications ».
        return []
    # Every member sees the default application(s) (the member app) without any grant,
    # PLUS any application explicitly granted to them and still active.
    rows = db.fetch_all(
        "SELECT a.code, a.nom, a.description, a.url, a.actif, a.est_defaut, "
        # ouvrable mirrors mon_acces_application: a default app is always open; any
        # other needs at least one access group tied to it. The card can then say
        # honestly "waiting for rights" instead of "active access" the gate refuses.
        "(a.est_defaut OR EXISTS (SELECT 1 FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        " WHERE mg.membre_id = %s AND mg.actif = true AND mg.application_code = a.code AND g.actif = true)) AS ouvrable "
        "FROM application a WHERE a.actif = true AND ("
        "  a.est_defaut = true "
        "  OR EXISTS (SELECT 1 FROM membre_application_acces m WHERE m.application_code = a.code AND m.membre_id = %s AND m.actif = true)"
        ") ORDER BY a.ordre, a.nom",
        (user.membre_id, user.membre_id),
        role=user.role,
    )
    return [
        ApplicationOut(
            code=r["code"], nom=r["nom"], description=r.get("description"), url=r.get("url"),
            actif=True, acces_actif=True, est_defaut=bool(r.get("est_defaut")), ouvrable=bool(r.get("ouvrable")),
        )
        for r in rows
    ]


@router.get("/membres/me/applications/{code}/acces")
def mon_acces_application(code: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, bool]:
    """Server-side gate a member app calls before opening. Enforces the two-step model:
    - default applications (the member space) are always open to a member;
    - any other application requires BOTH active visibility (LEVEL 1) AND at least one
      access group tied to that application (LEVEL 2). Visibility alone lets the member see
      the card but never connect: a member with no group cannot enter. The app must refuse
      the route/API when ``autorise`` is false."""
    if not user.membre_id:
        return {"autorise": False}
    app = db.fetch_one(
        "SELECT est_defaut FROM application WHERE code = %s AND actif = true",
        (code,),
        role=user.role,
    )
    if not app:
        return {"autorise": False}
    if app.get("est_defaut"):
        return {"autorise": True}
    row = db.fetch_one(
        "SELECT 1 FROM membre_application_acces m "
        "WHERE m.membre_id = %s AND m.application_code = %s AND m.actif = true "
        # The linked group must itself be ACTIVE: a deactivated group no longer
        # opens the door (it would grant a connection that carries no permission).
        "AND EXISTS (SELECT 1 FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = m.membre_id AND mg.actif = true AND mg.application_code = m.application_code AND g.actif = true)",
        (user.membre_id, code),
        role=user.role,
    )
    return {"autorise": bool(row)}


@router.get("/admin/applications", response_model=list[ApplicationOut])
def catalogue_applications(
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> list[ApplicationOut]:
    """The application catalogue, for the governance tabs (one tab per application)."""
    rows = db.fetch_all(
        "SELECT code, nom, description, url, actif, est_defaut FROM application WHERE actif = true ORDER BY ordre, nom",
        (), role=user.role,
    )
    return [ApplicationOut(code=r["code"], nom=r["nom"], description=r.get("description"), url=r.get("url"), actif=True, acces_actif=False, est_defaut=bool(r.get("est_defaut"))) for r in rows]


@router.get("/admin/membres/{membre_id}/applications", response_model=list[ApplicationOut])
def acces_membre(
    membre_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> list[ApplicationOut]:
    """Every application plus whether THIS member currently has an active grant, so the
    back office shows the full catalogue with each access state to grant or revoke."""
    if not db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
    rows = db.fetch_all(
        "SELECT a.code, a.nom, a.description, a.url, a.actif, a.est_defaut, "
        "COALESCE((SELECT m.actif FROM membre_application_acces m WHERE m.membre_id = %s AND m.application_code = a.code), false) AS acces_actif "
        # Inactive applications are included too: a grant that survives on a
        # deactivated application stays reviewable here instead of disappearing
        # from every surface (the member side still never lists inactive apps).
        "FROM application a ORDER BY a.actif DESC, a.ordre, a.nom",
        (membre_id,),
        role=user.role,
    )
    # A default application (est_defaut) is visible to EVERY member automatically
    # (mes_applications: est_defaut OR explicit grant), so its reported visibility is
    # always true regardless of any membre_application_acces row. Reporting it from
    # the grant table alone would wrongly show the member space as "not visible".
    return [
        ApplicationOut(
            code=r["code"], nom=r["nom"], description=r.get("description"), url=r.get("url"),
            actif=bool(r.get("actif")),
            acces_actif=bool(r.get("est_defaut")) or bool(r.get("acces_actif")),
            est_defaut=bool(r.get("est_defaut")),
        )
        for r in rows
    ]


class AccesIn(BaseModel):
    actif: bool
    motif: str | None = None


@router.put("/admin/membres/{membre_id}/applications/{code}")
def definir_acces(
    membre_id: str,
    code: str,
    payload: AccesIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))],
) -> dict[str, object]:
    """Grant (actif=true) or revoke (actif=false) a member's access to an application.
    Upsert on (membre_id, application_code): a single current state per pair, fully audited
    (who, when). Revoking flips the grant off; the member loses the application immediately
    on their next « Mes applications » load or secure session re-check."""
    if not db.fetch_one("SELECT id FROM membre WHERE id = %s", (membre_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
    app_row = db.fetch_one("SELECT code, est_defaut FROM application WHERE code = %s AND actif = true", (code,), role=user.role)
    if not app_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application inconnue")
    # A default application is visible to every member automatically: granting is
    # pointless and revoking would be a silent no-op (mes_applications short-circuits
    # on est_defaut). Refuse clearly instead of writing a misleading grant row.
    if bool(app_row.get("est_defaut")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="application par défaut : la visibilité est automatique pour tous les membres et ne se gère pas ici",
        )
    if payload.actif:
        db.execute(
            "INSERT INTO membre_application_acces (membre_id, application_code, actif, accorde_le, accorde_par, revoque_le, revoque_par, motif, maj_le) "
            "VALUES (%s, %s, true, now(), %s, NULL, NULL, %s, now()) "
            "ON CONFLICT (membre_id, application_code) DO UPDATE SET actif = true, accorde_le = now(), accorde_par = %s, revoque_le = NULL, revoque_par = NULL, motif = EXCLUDED.motif, maj_le = now()",
            (membre_id, code, user.id, payload.motif, user.id),
            role=user.role,
        )
        action = "octroi_acces_application"
    else:
        with db.connection(role=user.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO membre_application_acces (membre_id, application_code, actif, revoque_le, revoque_par, motif, maj_le) "
                "VALUES (%s, %s, false, now(), %s, %s, now()) "
                "ON CONFLICT (membre_id, application_code) DO UPDATE SET actif = false, revoque_le = now(), revoque_par = %s, motif = EXCLUDED.motif, maj_le = now()",
                (membre_id, code, user.id, payload.motif, user.id),
            )
            # Revoking visibility cascades to the rights: every group assigned FOR this
            # application is removed at once, so a member's rights never outlive the
            # visibility that justified them. Global assignments (application_code NULL,
            # from « Acces & groupes ») are left untouched. RETURNING itemises exactly which
            # groups were removed so the audit trail records the rights lost (RGPD).
            cur.execute(
                "DELETE FROM membre_groupe WHERE membre_id = %s AND application_code = %s RETURNING groupe_id",
                (membre_id, code),
            )
            groupes_retires = [str(r["groupe_id"]) for r in cur.fetchall()]
            # Audit inside the same transaction: the revocation and its trace commit together
            # or not at all, so the record of the withdrawn rights can never be lost (RGPD).
            audit.log_cursor(
                cur, user.id, user.role, "revocation_acces_application", "membre", membre_id,
                {"application": code, "actif": False, "groupes_retires": groupes_retires},
            )
        # AFTER the transaction commits: the cascade may have removed GLOBAL
        # memberships, so resync the cached account role from the now-committed state
        # (inside the transaction the separate connection would still see the old rows).
        from .groupes import _sync_account_role

        _sync_account_role(membre_id, user)
        return {"ok": True, "membre_id": membre_id, "application": code, "actif": False, "groupes_retires": groupes_retires}
    audit.log(user.id, user.role, action, "membre", membre_id, {"application": code, "actif": payload.actif})
    return {"ok": True, "membre_id": membre_id, "application": code, "actif": payload.actif}


# --- LEVEL 2: application roles (per-app tab in the governance page) -----------------

class RoleApplicationOut(BaseModel):
    code: str
    nom: str
    description: str | None = None


class MembreAvecAcces(BaseModel):
    membre_id: str
    nom: str
    matricule: str | None = None
    roles: list[str] = []  # role codes held by this member for this application


@router.get("/admin/applications/{code}/roles", response_model=list[RoleApplicationOut])
def roles_application(
    code: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> list[RoleApplicationOut]:
    """The roles available for one application, to assign in its governance tab."""
    rows = db.fetch_all(
        "SELECT code, nom, description FROM application_role WHERE application_code = %s ORDER BY ordre, nom",
        (code,), role=user.role,
    )
    return [RoleApplicationOut(code=r["code"], nom=r["nom"], description=r.get("description")) for r in rows]


@router.get("/admin/applications/{code}/membres", response_model=list[MembreAvecAcces])
def membres_avec_acces(
    code: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> list[MembreAvecAcces]:
    """Members who currently HAVE access to this application (LEVEL 1), each with the
    roles they hold for it (LEVEL 2), so the app tab shows exactly who can see it and
    what they can do. Access comes first: a member without access is not listed here."""
    if not db.fetch_one("SELECT code FROM application WHERE code = %s", (code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application inconnue")
    rows = db.fetch_all(
        "SELECT m.id AS membre_id, m.matricule, TRIM(COALESCE(m.prenoms,'') || ' ' || COALESCE(m.nom,'')) AS nom, "
        "COALESCE((SELECT array_agg(r.role_code) FROM membre_application_role r WHERE r.membre_id = m.id AND r.application_code = %s), '{}') AS roles "
        "FROM membre_application_acces a JOIN membre m ON m.id = a.membre_id "
        "WHERE a.application_code = %s AND a.actif = true "
        "ORDER BY lower(m.nom), lower(m.prenoms), m.matricule",
        (code, code), role=user.role,
    )
    return [
        MembreAvecAcces(
            membre_id=str(r["membre_id"]),
            nom=(str(r.get("nom") or "").strip() or (r.get("matricule") or str(r["membre_id"]))),
            matricule=r.get("matricule"),
            roles=[str(x) for x in (r.get("roles") or [])],
        )
        for r in rows
    ]


@router.put("/admin/membres/{membre_id}/applications/{code}/roles/{role_code}")
def definir_role(
    membre_id: str,
    code: str,
    role_code: str,
    payload: AccesIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))],
) -> dict[str, object]:
    """Assign (actif=true) or remove (actif=false) an application role for a member. A role
    can only be assigned once the member HAS access to the application (access first, then
    rights). Every change is audited."""
    if not db.fetch_one(
        "SELECT 1 FROM membre_application_acces WHERE membre_id = %s AND application_code = %s AND actif = true",
        (membre_id, code), role=user.role,
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="accordez d'abord l'acces a l'application, puis le role")
    if not db.fetch_one("SELECT 1 FROM application_role WHERE application_code = %s AND code = %s", (code, role_code), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="role inconnu pour cette application")
    if payload.actif:
        db.execute(
            "INSERT INTO membre_application_role (membre_id, application_code, role_code, accorde_le, accorde_par) "
            "VALUES (%s, %s, %s, now(), %s) ON CONFLICT (membre_id, application_code, role_code) DO NOTHING",
            (membre_id, code, role_code, user.id), role=user.role,
        )
        action = "octroi_role_application"
    else:
        db.execute(
            "DELETE FROM membre_application_role WHERE membre_id = %s AND application_code = %s AND role_code = %s",
            (membre_id, code, role_code), role=user.role,
        )
        action = "retrait_role_application"
    audit.log(user.id, user.role, action, "membre", membre_id, {"application": code, "role": role_code, "actif": payload.actif})
    return {"ok": True, "membre_id": membre_id, "application": code, "role": role_code, "actif": payload.actif}


# --- LEVEL 2 (real): assign EXISTING access groups per application ------------------

class GroupeAssigneOut(BaseModel):
    membre_groupe_id: str
    groupe_id: str
    cle: str
    libelle: str
    # The granted platform role (or 'membre' for a permission-mode group), shown so
    # the operator sees that these rights apply GLOBALLY, not only to this app.
    role_accorde: str = "membre"
    # A deactivated group grants nothing and no longer opens the application.
    groupe_actif: bool = True


@router.get("/admin/membres/{membre_id}/applications/{code}/groupes", response_model=list[GroupeAssigneOut])
def groupes_membre_application(
    membre_id: str, code: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> list[GroupeAssigneOut]:
    """The access groups assigned to this member FOR this application (the rights tied to
    this application, removed automatically when the application access is revoked)."""
    rows = db.fetch_all(
        "SELECT mg.id AS membre_groupe_id, g.id AS groupe_id, g.cle, g.libelle, g.role_accorde, g.actif AS groupe_actif "
        "FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "WHERE mg.membre_id = %s AND mg.actif = true AND mg.application_code = %s ORDER BY g.libelle",
        (membre_id, code), role=user.role,
    )
    return [
        GroupeAssigneOut(
            membre_groupe_id=str(r["membre_groupe_id"]), groupe_id=str(r["groupe_id"]),
            cle=r["cle"], libelle=r["libelle"],
            role_accorde=str(r.get("role_accorde") or "membre"),
            groupe_actif=bool(r.get("groupe_actif")),
        )
        for r in rows
    ]


@router.put("/admin/membres/{membre_id}/applications/{code}/groupes/{groupe_id}")
def definir_groupe_application(
    membre_id: str,
    code: str,
    groupe_id: str,
    payload: AccesIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("acces.administrer"))],
) -> dict[str, object]:
    """Add (actif=true) or remove (actif=false) an EXISTING access group for a member,
    tied to this application. Rights require visibility first: a group can only be added
    once the member has active access to the application. The visibility check and the
    write run in a SINGLE transaction that locks the visibility row (FOR UPDATE), so a
    concurrent revocation cannot interleave to leave a right without its visibility; the
    database trigger (migration 0138) is the final backstop. Audited."""
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        if payload.actif:
            # Lock the visibility row: a concurrent revocation (which flips it inactive and
            # cascade-deletes the groups) blocks until this transaction commits, so the
            # two-step invariant holds even under a race.
            cur.execute(
                "SELECT 1 FROM membre_application_acces WHERE membre_id = %s AND application_code = %s AND actif = true FOR UPDATE",
                (membre_id, code),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="accordez d'abord la visibilite de l'application, puis les droits")
            cur.execute(
                "SELECT id, mode, role_accorde FROM groupe_acces WHERE id = %s AND actif = true",
                (groupe_id,),
            )
            groupe = cur.fetchone()
            if not groupe:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="groupe inconnu")
            # Same least-privilege guard as the direct route. Without it this tab was
            # a way round it: an administrator could hand themselves the group that
            # grants super_admin, and since a tagged membership no longer raises the
            # account role, they would exercise that set without even showing up as
            # platform staff. Granting a group is granting a group, whichever screen
            # it is done from.
            _assert_peut_gerer(user, str(groupe.get("role_accorde") or "membre"), membre_id)
            if str(groupe.get("mode")) == "permissions":
                cur.execute("SELECT permission FROM groupe_permission WHERE groupe_id = %s", (groupe_id,))
                from .groupes import _assert_peut_accorder_permissions

                _assert_peut_accorder_permissions([str(p["permission"]) for p in cur.fetchall()], user)
            cur.execute("SELECT 1 FROM membre_groupe WHERE membre_id = %s AND groupe_id = %s AND application_code = %s", (membre_id, groupe_id, code))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO membre_groupe (membre_id, groupe_id, application_code, portee_type, portee_id, ajoute_par) "
                    "VALUES (%s, %s, %s, 'global', NULL, %s)",
                    (membre_id, groupe_id, code, user.id),
                )
            action = "octroi_groupe_application"
        else:
            cur.execute(
                "DELETE FROM membre_groupe WHERE membre_id = %s AND groupe_id = %s AND application_code = %s",
                (membre_id, groupe_id, code),
            )
            action = "retrait_groupe_application"
    # The membership just granted/removed is GLOBAL (portee_type='global'), so it feeds
    # both _effective_role and the permission enforcement. Resync the cached account
    # role: without this, a member granted Administration through an application tab
    # kept role='membre' (absent from the platform roster, staff 2FA not applied)
    # while actually holding every admin permission.
    from .groupes import _sync_account_role

    eff, _ = _sync_account_role(membre_id, user)
    audit.log(user.id, user.role, action, "membre", membre_id, {"application": code, "groupe": groupe_id, "actif": payload.actif, "effective_role": eff})
    return {"ok": True, "membre_id": membre_id, "application": code, "groupe": groupe_id, "actif": payload.actif, "effective_role": eff}
