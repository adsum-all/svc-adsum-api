"""Committee collaboration spaces (Espaces), served from the real PostgreSQL.

The space layer the collaboration prototype introduced: a space groups boards,
carries its own labels and a membership with a role (proprietaire / admin /
membre / observateur), plus access requests. Backs the collaboration app's
``lib/store`` contract. Reserved to the committee roles by RLS; the finer
space-level role is enforced here on top. The collaborator identity is the login
account (``utilisateur``), consistent with migrations 0097/0098.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, collaboration_notif, db
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-espaces"])

ADMINS = ("super_admin", "admin")
GERANTS = ("proprietaire", "admin")
DEFAULT_ETIQUETTES = (
    ("Liturgie", "#2A4FAD", True),
    ("Intendance", "#2E7D32", False),
    ("Communication", "#8E44AD", False),
    ("Urgent", "#C0392B", False),
    ("Finance", "#B8860B", False),
)


class MembreEspaceOut(BaseModel):
    membre_id: str
    role: str
    nom: str = ""
    initiales: str = ""


class EtiquetteOut(BaseModel):
    id: str
    nom: str
    couleur: str
    par_defaut: bool


class DemandeAccesOut(BaseModel):
    id: str
    membre_id: str
    cree_le: str


class SousEspaceOut(BaseModel):
    """A lightweight sub-space entry listed under its parent (no members payload)."""

    id: str
    nom: str
    couleur: str
    initiale: str
    archive: bool


class EspaceOut(BaseModel):
    id: str
    nom: str
    description: str
    type: str
    couleur: str
    initiale: str
    membres: list[MembreEspaceOut]
    etiquettes: list[EtiquetteOut]
    observateurs_commentent: bool
    archive: bool
    invitation_jeton: str
    demandes_acces: list[DemandeAccesOut]
    # Hierarchy: the parent workspace (null for a top-level space) and, for a
    # top-level space, its sub-spaces. A sub-space never has sub-spaces (depth 1).
    parent_id: str | None = None
    sous_espaces: list[SousEspaceOut] = []
    # Dedicated instruction bot (general workspaces only), strictly separate from the
    # member notification bot. Status: non_configure / demande / configure. The deep
    # link points at THIS workspace's own bot and is set only once it is configured.
    instruction_bot_statut: str = "non_configure"
    instruction_bot_username: str | None = None
    telegram_deep_link: str | None = None


class MembreOut(BaseModel):
    id: str
    nom: str
    courriel: str
    initiales: str


class EspaceIn(BaseModel):
    nom: TitleStr
    description: TextStr | None = ""
    type: ShortStr = "autre"
    couleur: ShortStr = "#2A4FAD"
    # Optional parent workspace: when set, this space is created as a sub-space of
    # it. The parent must itself be top-level (depth is capped at one).
    parent_id: ShortStr | None = None


class EspacePatch(BaseModel):
    nom: TitleStr | None = None
    description: TextStr | None = None
    observateurs_commentent: bool | None = None
    archive: bool | None = None


class MembreIn(BaseModel):
    membre_id: ShortStr
    role: ShortStr = "membre"


class RolePatch(BaseModel):
    role: ShortStr


class AccepterIn(BaseModel):
    role: ShortStr = "membre"


class EtiquetteIn(BaseModel):
    nom: LineStr
    couleur: ShortStr = "#2A4FAD"
    par_defaut: bool = False


class EtiquettePatch(BaseModel):
    nom: LineStr | None = None
    couleur: ShortStr | None = None
    par_defaut: bool | None = None


def _name_from_email(email: str) -> str:
    local = (email.split("@")[0] or "").strip()
    if not local:
        return "Membre du comite"
    return " ".join(part.capitalize() for part in local.replace(".", " ").replace("-", " ").replace("_", " ").split())


def _initials(nom: str) -> str:
    parts = [p for p in nom.split() if p]
    return ("".join(p[0] for p in parts[:2]).upper()) or "AD"


def role_in_espace(espace_id: str, user: UserMe) -> str | None:
    """The signed-in user's role inside the space. A TECHNICAL super-admin (support
    identity with ``acces_technique_global``) acts as owner of every workspace WITHOUT
    membership: it must reach all content it maintains, across the platform. Everyone
    else, including a member super-admin, is by explicit membership only: a back-office
    role grants no implicit content access. Governance actions use
    ``require_espace_gouvernance`` instead."""
    if user.acces_technique_global:
        return "proprietaire"
    row = db.fetch_one(
        "SELECT role FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, user.id),
        role=user.role,
    )
    return row["role"] if row else None


def require_espace_role(espace_id: str, user: UserMe, allowed: tuple[str, ...]) -> str:
    """Content guard: the caller must be an explicit member of the space with a
    sufficient role. Used for boards, cards and canal content."""
    role = role_in_espace(espace_id, user)
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a member of this space")
    if role not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient space role")
    return role


def require_espace_gouvernance(espace_id: str, user: UserMe, allowed: tuple[str, ...]) -> str:
    """Governance guard for back-office administration of a workspace (manage members
    and roles, configure/dissociate its bot, delete/archive it). A platform admin
    (super_admin/admin) may govern ANY workspace WITHOUT being a member: this is access
    governance, not content, and never grants visibility of the workspace's private
    canal, boards or files. Otherwise the caller must be a workspace gerant."""
    if user.role in ADMINS:
        return "gouvernance"
    return require_espace_role(espace_id, user, allowed)


def _espace_out(espace_id: str, role: str, gerant: bool = True) -> EspaceOut:
    # ``gerant`` gates the manager-only fields (the invitation token and the pending
    # access requests): a plain member or observer of a space must never read its
    # invitation link nor the list of people who requested access.
    e = db.fetch_one(
        "SELECT id, nom, description, type, couleur, initiale, observateurs_commentent, archive, "
        "invitation_jeton, parent_id, telegram_intake_token, "
        "instruction_bot_statut, instruction_bot_username FROM collab_espace WHERE id = %s",
        (espace_id,),
        role=role,
    )
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="space not found")
    # Sub-spaces of a top-level space (depth 1: a sub-space never lists its own).
    sous = db.fetch_all(
        "SELECT id, nom, couleur, initiale, archive FROM collab_espace "
        "WHERE parent_id = %s AND supprime_le IS NULL ORDER BY cree_le",
        (espace_id,),
        role=role,
    ) if not e.get("parent_id") else []
    membres = db.fetch_all(
        "SELECT em.utilisateur_id, em.role, u.email, m.nom_affiche "
        "FROM collab_espace_membre em JOIN utilisateur u ON u.id = em.utilisateur_id "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE em.espace_id = %s ORDER BY em.ajoute_le",
        (espace_id,),
        role=role,
    )
    etiquettes = db.fetch_all(
        "SELECT id, nom, couleur, par_defaut FROM collab_etiquette WHERE espace_id = %s ORDER BY position, nom",
        (espace_id,),
        role=role,
    )
    demandes = db.fetch_all(
        "SELECT id, utilisateur_id, cree_le FROM collab_demande_acces WHERE espace_id = %s ORDER BY cree_le",
        (espace_id,),
        role=role,
    )
    return EspaceOut(
        id=str(e["id"]),
        nom=e["nom"],
        description=e["description"] or "",
        type=e["type"],
        couleur=e["couleur"],
        initiale=e["initiale"],
        membres=[
            MembreEspaceOut(
                membre_id=str(m["utilisateur_id"]),
                role=m["role"],
                nom=(m["nom_affiche"] or _name_from_email(m["email"])),
                initiales=_initials(m["nom_affiche"] or _name_from_email(m["email"])),
            )
            for m in membres
        ],
        etiquettes=[
            EtiquetteOut(id=str(x["id"]), nom=x["nom"], couleur=x["couleur"], par_defaut=x["par_defaut"])
            for x in etiquettes
        ],
        observateurs_commentent=e["observateurs_commentent"],
        archive=e["archive"],
        invitation_jeton=(e["invitation_jeton"] if gerant else ""),
        demandes_acces=([
            DemandeAccesOut(
                id=str(d["id"]),
                membre_id=str(d["utilisateur_id"]),
                cree_le=d["cree_le"].isoformat() if d["cree_le"] else "",
            )
            for d in demandes
        ] if gerant else []),
        parent_id=(str(e["parent_id"]) if e.get("parent_id") else None),
        sous_espaces=[
            SousEspaceOut(
                id=str(s["id"]), nom=s["nom"], couleur=s["couleur"], initiale=s["initiale"], archive=s["archive"]
            )
            for s in sous
        ],
        instruction_bot_statut=(e.get("instruction_bot_statut") or "non_configure"),
        instruction_bot_username=(e.get("instruction_bot_username") or None),
        telegram_deep_link=_telegram_deep_link(
            e.get("telegram_intake_token"), e.get("instruction_bot_username"), e.get("instruction_bot_statut")
        ),
    )


def _telegram_deep_link(token: object, bot_username: object, statut: object) -> str | None:
    """Deep link a member opens on THIS workspace's dedicated instruction bot. Set
    only when the workspace's own bot is configured; never the member notification
    bot. None for a sub-space (no intake token) or an unconfigured bot."""
    if statut != "configure" or not token or not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=canal_{token}"


@router.get("/membres", response_model=list[MembreOut])
def list_membres(user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]) -> list[MembreOut]:
    """The real people who can collaborate (be added to a space, assigned, mentioned).

    Only accounts linked to an actual member (membre_id NOT NULL) are listed:
    technical/application accounts (a super-admin or service account with no member
    record) run the platform but are never a person to add or assign in a space, so
    they are excluded from this directory. They are managed in the back-office
    accounts settings, not here.
    """
    rows = db.fetch_all(
        "SELECT u.id, u.email, m.nom_affiche FROM utilisateur u JOIN membre m ON m.id = u.membre_id "
        "WHERE u.role IN ('super_admin', 'admin', 'gestionnaire', 'direction') AND u.actif = true "
        "ORDER BY u.email",
        (),
        role=user.role,
    )
    out: list[MembreOut] = []
    for r in rows:
        nom = r["nom_affiche"] or _name_from_email(r["email"])
        out.append(MembreOut(id=str(r["id"]), nom=nom, courriel=r["email"], initiales=_initials(nom)))
    return out


@router.get("/espaces", response_model=list[EspaceOut])
def list_espaces(user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]) -> list[EspaceOut]:
    # The collaboration app's own workspace list. Membership only, for EVERYONE
    # including a member super_admin: a back-office role never reveals a workspace the
    # caller was not explicitly added to. A technical super-admin (acces_technique_global)
    # is the sole exception: as a support identity it sees every top-level workspace, so
    # it can reach the content it maintains. Governance oversight of ALL workspaces for a
    # member admin remains the separate GET /admin/collaboration/espaces endpoint.
    if user.acces_technique_global:
        rows = db.fetch_all(
            "SELECT id FROM collab_espace WHERE parent_id IS NULL AND NOT archive AND supprime_le IS NULL "
            "ORDER BY cree_le DESC",
            (), role=user.role,
        )
    else:
        rows = db.fetch_all(
            "SELECT e.id FROM collab_espace e JOIN collab_espace_membre m ON m.espace_id = e.id "
            "WHERE m.utilisateur_id = %s AND NOT e.archive AND e.supprime_le IS NULL "
            "AND (e.parent_id IS NULL OR NOT EXISTS ("
            "  SELECT 1 FROM collab_espace_membre pm WHERE pm.espace_id = e.parent_id AND pm.utilisateur_id = %s)) "
            "ORDER BY e.cree_le DESC",
            (user.id, user.id),
            role=user.role,
        )
    # The list never carries the manager-only fields (invitation token, access requests):
    # those are read from the space detail (get_espace) by a manager only.
    return [_espace_out(str(r["id"]), user.role, gerant=False) for r in rows]


@router.get("/espaces/archives", response_model=list[EspaceOut])
def list_espaces_archives(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> list[EspaceOut]:
    """Top-level ARCHIVED workspaces the caller belongs to (archive=true, not trashed),
    so the collaboration app can list them and offer an unarchive action. Same membership
    scoping as ``list_espaces``; a technical super-admin sees every archived top-level
    workspace. Route declared before ``/espaces/{espace_id}`` so 'archives' is never read
    as an id."""
    if user.acces_technique_global:
        rows = db.fetch_all(
            "SELECT id FROM collab_espace WHERE parent_id IS NULL AND archive "
            "AND supprime_le IS NULL ORDER BY cree_le DESC",
            (), role=user.role,
        )
    else:
        rows = db.fetch_all(
            "SELECT e.id FROM collab_espace e JOIN collab_espace_membre m ON m.espace_id = e.id "
            "WHERE m.utilisateur_id = %s AND e.parent_id IS NULL AND e.archive AND e.supprime_le IS NULL "
            "ORDER BY e.cree_le DESC",
            (user.id,), role=user.role,
        )
    return [_espace_out(str(r["id"]), user.role, gerant=False) for r in rows]


@router.get("/admin/espaces", response_model=list[EspaceOut])
def list_espaces_gouvernance(
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))],
) -> list[EspaceOut]:
    """Back-office GOVERNANCE view: every workspace's metadata (name, members, roles,
    access requests) so the control tower can supervise and assign access. This lists
    all workspaces on purpose, so it is guarded by the access-governance permission
    (``acces.administrer``), NOT the member-facing ``collaboration.superviser`` which
    every collaborator holds: a plain member must never be able to enumerate every
    workspace here. It is a governance surface, NOT content: it never exposes the canal
    instructions, notes, cards or files of a workspace. Seeing that private content
    still requires membership (enforced in the canal/board endpoints)."""
    rows = db.fetch_all(
        "SELECT id FROM collab_espace WHERE supprime_le IS NULL AND parent_id IS NULL ORDER BY cree_le DESC",
        (), role=user.role,
    )
    return [_espace_out(str(r["id"]), user.role) for r in rows]


@router.get("/espaces/{espace_id}", response_model=EspaceOut)
def get_espace(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> EspaceOut:
    role = require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    gerant = role in GERANTS or user.role in ADMINS or user.acces_technique_global
    return _espace_out(espace_id, user.role, gerant=gerant)


@router.post("/espaces", response_model=EspaceOut, status_code=status.HTTP_201_CREATED)
def create_espace(
    payload: EspaceIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> EspaceOut:
    parent_id: str | None = None
    if payload.parent_id:
        parent_id = str(payload.parent_id)
        # The caller must manage the parent, and the parent must be top-level: a
        # sub-space cannot itself have sub-spaces (hierarchy is capped at one level).
        require_espace_role(parent_id, user, GERANTS)
        parent = db.fetch_one("SELECT parent_id FROM collab_espace WHERE id = %s", (parent_id,), role=user.role)
        if not parent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="parent space not found")
        if parent.get("parent_id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Un sous-espace ne peut pas contenir d'autres sous-espaces (un seul niveau).",
            )
    initiale = (payload.nom[:1] or "E").upper()
    # A general workspace gets its own Telegram intake token (members route their
    # voice notes to it); a sub-space never does (it shares its parent's intake).
    intake = "gen_random_uuid()::text" if parent_id is None else "NULL"
    created = db.execute(
        "INSERT INTO collab_espace "
        "(nom, description, type, couleur, initiale, invitation_jeton, cree_par, parent_id, telegram_intake_token) "
        f"VALUES (%s, %s, %s, %s, %s, gen_random_uuid()::text, %s, %s, {intake}) RETURNING id",
        (payload.nom, payload.description or "", payload.type, payload.couleur, initiale, user.id, parent_id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="space not created")
    espace_id = str(created["id"])
    db.execute(
        "INSERT INTO collab_espace_membre (espace_id, utilisateur_id, role) VALUES (%s, %s, 'proprietaire')",
        (espace_id, user.id),
        role=user.role,
    )
    for position, (nom, couleur, par_defaut) in enumerate(DEFAULT_ETIQUETTES):
        db.execute(
            "INSERT INTO collab_etiquette (espace_id, nom, couleur, par_defaut, position) VALUES (%s, %s, %s, %s, %s)",
            (espace_id, nom, couleur, par_defaut, position),
            role=user.role,
        )
    audit.log(user.id, user.role, "collab_espace_creation", "collab_espace", espace_id,
              {"nom": payload.nom, "type": payload.type})
    return _espace_out(espace_id, user.role)


@router.patch("/espaces/{espace_id}", response_model=EspaceOut)
def update_espace(
    espace_id: str,
    payload: EspacePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return _espace_out(espace_id, user.role)
    sets = ", ".join(f"{key} = %s" for key in fields)
    params = [*fields.values(), espace_id]
    updated = db.execute(
        f"UPDATE collab_espace SET {sets}, maj_le = now() WHERE id = %s RETURNING id",
        tuple(params),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="space not found")
    audit.log(user.id, user.role, "collab_espace_maj", "collab_espace", espace_id, {"champs": sorted(fields)})
    return _espace_out(espace_id, user.role)


@router.post("/espaces/{espace_id}/membres", response_model=EspaceOut, status_code=status.HTTP_201_CREATED)
def add_membre(
    espace_id: str,
    payload: MembreIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_gouvernance(espace_id, user, GERANTS)
    db.execute(
        "INSERT INTO collab_espace_membre (espace_id, utilisateur_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO UPDATE SET role = EXCLUDED.role",
        (espace_id, payload.membre_id, payload.role),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_membre_ajout", "collab_espace", espace_id,
              {"membre": str(payload.membre_id), "role": payload.role})
    return _espace_out(espace_id, user.role)


def _refuser_si_dernier_proprietaire(espace_id: str, membre_id: str, role: str) -> None:
    """Never leave a workspace without an owner: refuse (409) to demote or remove the
    sole ``proprietaire``. Recovery of an owner-less space would otherwise require the
    admin governance surface."""
    row = db.fetch_one(
        "SELECT (SELECT count(*) FROM collab_espace_membre WHERE espace_id = %s AND role = 'proprietaire') AS n, "
        "(SELECT role FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s) AS r",
        (espace_id, espace_id, membre_id), role=role,
    )
    if row and row.get("r") == "proprietaire" and int(row.get("n") or 0) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="le dernier propriétaire ne peut pas être retiré ou rétrogradé",
        )


@router.patch("/espaces/{espace_id}/membres/{membre_id}", response_model=EspaceOut)
def change_role(
    espace_id: str,
    membre_id: str,
    payload: RolePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_gouvernance(espace_id, user, GERANTS)
    if payload.role != "proprietaire":
        _refuser_si_dernier_proprietaire(espace_id, membre_id, user.role)
    db.execute(
        "UPDATE collab_espace_membre SET role = %s WHERE espace_id = %s AND utilisateur_id = %s",
        (payload.role, espace_id, membre_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_membre_role", "collab_espace", espace_id,
              {"membre": membre_id, "role": payload.role})
    return _espace_out(espace_id, user.role)


@router.delete("/espaces/{espace_id}/membres/{membre_id}", response_model=EspaceOut)
def remove_membre(
    espace_id: str,
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_gouvernance(espace_id, user, GERANTS)
    _refuser_si_dernier_proprietaire(espace_id, membre_id, user.role)
    db.execute(
        "DELETE FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, membre_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_membre_retrait", "collab_espace", espace_id,
              {"membre": membre_id})
    return _espace_out(espace_id, user.role)


class RejoindreOut(BaseModel):
    espace_id: str
    espace_nom: str
    statut: str


@router.post("/rejoindre/{jeton}", response_model=RejoindreOut)
def rejoindre(
    jeton: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> RejoindreOut:
    """Follow an invitation link: resolve the space by its invitation token and
    file an access request for the signed-in member (or report that they are
    already a member). Feeds the space "access requests" panel."""
    e = db.fetch_one(
        "SELECT id, nom FROM collab_espace WHERE invitation_jeton = %s AND NOT archive", (jeton,), role=user.role
    )
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid invitation")
    espace_id = str(e["id"])
    if role_in_espace(espace_id, user) is not None:
        return RejoindreOut(espace_id=espace_id, espace_nom=e["nom"], statut="deja_membre")
    inserted = db.execute(
        "INSERT INTO collab_demande_acces (espace_id, utilisateur_id) VALUES (%s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO NOTHING RETURNING id",
        (espace_id, user.id),
        role=user.role,
    )
    # Only tell the owners/admins on a genuinely new request, never on a duplicate.
    if inserted:
        collaboration_notif.notifier_demande_acces(espace_id, str(e["nom"]), user.id, user.role)
    return RejoindreOut(espace_id=espace_id, espace_nom=e["nom"], statut="demande")


@router.post("/espaces/{espace_id}/demandes", response_model=RejoindreOut, status_code=status.HTTP_201_CREATED)
def demander_acces(
    espace_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> RejoindreOut:
    """Request access to a workspace for the SIGNED-IN caller only. It never files a
    request on behalf of an arbitrary user, and never returns the workspace detail to a
    non-member (that would leak members, the invitation token and pending requests): it
    answers a minimal acknowledgement, exactly like following an invitation link."""
    e = db.fetch_one(
        "SELECT nom FROM collab_espace WHERE id = %s AND supprime_le IS NULL", (espace_id,), role=user.role
    )
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="space not found")
    if role_in_espace(espace_id, user) is not None:
        return RejoindreOut(espace_id=espace_id, espace_nom=str(e["nom"]), statut="deja_membre")
    inserted = db.execute(
        "INSERT INTO collab_demande_acces (espace_id, utilisateur_id) VALUES (%s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO NOTHING RETURNING id",
        (espace_id, user.id),
        role=user.role,
    )
    if inserted:
        collaboration_notif.notifier_demande_acces(espace_id, str(e["nom"]), user.id, user.role)
    return RejoindreOut(espace_id=espace_id, espace_nom=str(e["nom"]), statut="demande")


@router.post("/espaces/{espace_id}/demandes/{demande_id}/accepter", response_model=EspaceOut)
def accepter_demande(
    espace_id: str,
    demande_id: str,
    payload: AccepterIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_gouvernance(espace_id, user, GERANTS)
    demande = db.fetch_one(
        "SELECT utilisateur_id FROM collab_demande_acces WHERE id = %s AND espace_id = %s",
        (demande_id, espace_id),
        role=user.role,
    )
    if not demande:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    db.execute(
        "INSERT INTO collab_espace_membre (espace_id, utilisateur_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO UPDATE SET role = EXCLUDED.role",
        (espace_id, str(demande["utilisateur_id"]), payload.role),
        role=user.role,
    )
    db.execute("DELETE FROM collab_demande_acces WHERE id = %s", (demande_id,), role=user.role)
    audit.log(user.id, user.role, "collab_demande_acceptee", "collab_espace", espace_id,
              {"membre": str(demande["utilisateur_id"]), "role": payload.role})
    return _espace_out(espace_id, user.role)


@router.delete("/espaces/{espace_id}/demandes/{demande_id}", response_model=EspaceOut)
def refuser_demande(
    espace_id: str,
    demande_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_gouvernance(espace_id, user, GERANTS)
    db.execute(
        "DELETE FROM collab_demande_acces WHERE id = %s AND espace_id = %s",
        (demande_id, espace_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_demande_refusee", "collab_espace", espace_id, {"demande": demande_id})
    return _espace_out(espace_id, user.role)


@router.post("/espaces/{espace_id}/etiquettes", response_model=EtiquetteOut, status_code=status.HTTP_201_CREATED)
def create_etiquette(
    espace_id: str,
    payload: EtiquetteIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EtiquetteOut:
    require_espace_role(espace_id, user, GERANTS)
    created = db.execute(
        "INSERT INTO collab_etiquette (espace_id, nom, couleur, par_defaut, position) "
        "VALUES (%s, %s, %s, %s, (SELECT coalesce(max(position), -1) + 1 FROM collab_etiquette WHERE espace_id = %s)) "
        "RETURNING id, nom, couleur, par_defaut",
        (espace_id, payload.nom, payload.couleur, payload.par_defaut, espace_id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="label not created")
    return EtiquetteOut(
        id=str(created["id"]), nom=created["nom"], couleur=created["couleur"], par_defaut=created["par_defaut"]
    )


@router.patch("/espaces/{espace_id}/etiquettes/{etiquette_id}", response_model=EtiquetteOut)
def update_etiquette(
    espace_id: str,
    etiquette_id: str,
    payload: EtiquettePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EtiquetteOut:
    require_espace_role(espace_id, user, GERANTS)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    sets = ", ".join(f"{key} = %s" for key in fields)
    params = [*fields.values(), etiquette_id, espace_id]
    updated = db.execute(
        f"UPDATE collab_etiquette SET {sets} WHERE id = %s AND espace_id = %s "
        "RETURNING id, nom, couleur, par_defaut",
        tuple(params),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="label not found")
    return EtiquetteOut(
        id=str(updated["id"]), nom=updated["nom"], couleur=updated["couleur"], par_defaut=updated["par_defaut"]
    )


@router.delete("/espaces/{espace_id}/etiquettes/{etiquette_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_etiquette(
    espace_id: str,
    etiquette_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    require_espace_role(espace_id, user, GERANTS)
    db.execute(
        "DELETE FROM collab_etiquette WHERE id = %s AND espace_id = %s",
        (etiquette_id, espace_id),
        role=user.role,
    )
