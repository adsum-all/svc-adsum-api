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

from . import db
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-espaces"])

ADMINS = ("super_admin", "admin")
GERANTS = ("proprietaire", "admin")
DEFAULT_ETIQUETTES = (("General", "#2A4FAD", True), ("Urgent", "#C0392B", False))


class MembreEspaceOut(BaseModel):
    membre_id: str
    role: str


class EtiquetteOut(BaseModel):
    id: str
    nom: str
    couleur: str
    par_defaut: bool


class DemandeAccesOut(BaseModel):
    id: str
    membre_id: str
    cree_le: str


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


class DemandeIn(BaseModel):
    membre_id: ShortStr


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
    """The signed-in user's role inside the space. Admins act as owners over
    every space (the back office controls everything)."""
    if user.role in ADMINS:
        return "proprietaire"
    row = db.fetch_one(
        "SELECT role FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, user.id),
        role=user.role,
    )
    return row["role"] if row else None


def require_espace_role(espace_id: str, user: UserMe, allowed: tuple[str, ...]) -> str:
    role = role_in_espace(espace_id, user)
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a member of this space")
    if role not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient space role")
    return role


def _espace_out(espace_id: str, role: str) -> EspaceOut:
    e = db.fetch_one(
        "SELECT id, nom, description, type, couleur, initiale, observateurs_commentent, archive, invitation_jeton "
        "FROM collab_espace WHERE id = %s",
        (espace_id,),
        role=role,
    )
    if not e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="space not found")
    membres = db.fetch_all(
        "SELECT utilisateur_id, role FROM collab_espace_membre WHERE espace_id = %s ORDER BY ajoute_le",
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
        membres=[MembreEspaceOut(membre_id=str(m["utilisateur_id"]), role=m["role"]) for m in membres],
        etiquettes=[
            EtiquetteOut(id=str(x["id"]), nom=x["nom"], couleur=x["couleur"], par_defaut=x["par_defaut"])
            for x in etiquettes
        ],
        observateurs_commentent=e["observateurs_commentent"],
        archive=e["archive"],
        invitation_jeton=e["invitation_jeton"],
        demandes_acces=[
            DemandeAccesOut(
                id=str(d["id"]),
                membre_id=str(d["utilisateur_id"]),
                cree_le=d["cree_le"].isoformat() if d["cree_le"] else "",
            )
            for d in demandes
        ],
    )


@router.get("/membres", response_model=list[MembreOut])
def list_membres(user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]) -> list[MembreOut]:
    """The committee accounts that can collaborate (be added to a space, assigned,
    mentioned). Display name comes from the linked member, else the e-mail."""
    rows = db.fetch_all(
        "SELECT u.id, u.email, m.nom_affiche FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id "
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
    if user.role in ADMINS:
        rows = db.fetch_all(
            "SELECT id FROM collab_espace WHERE NOT archive ORDER BY cree_le DESC", (), role=user.role
        )
    else:
        rows = db.fetch_all(
            "SELECT e.id FROM collab_espace e JOIN collab_espace_membre m ON m.espace_id = e.id "
            "WHERE m.utilisateur_id = %s AND NOT e.archive ORDER BY e.cree_le DESC",
            (user.id,),
            role=user.role,
        )
    return [_espace_out(str(r["id"]), user.role) for r in rows]


@router.get("/espaces/{espace_id}", response_model=EspaceOut)
def get_espace(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> EspaceOut:
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return _espace_out(espace_id, user.role)


@router.post("/espaces", response_model=EspaceOut, status_code=status.HTTP_201_CREATED)
def create_espace(
    payload: EspaceIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> EspaceOut:
    initiale = (payload.nom[:1] or "E").upper()
    created = db.execute(
        "INSERT INTO collab_espace (nom, description, type, couleur, initiale, invitation_jeton, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, gen_random_uuid()::text, %s) RETURNING id",
        (payload.nom, payload.description or "", payload.type, payload.couleur, initiale, user.id),
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
    return _espace_out(espace_id, user.role)


@router.post("/espaces/{espace_id}/membres", response_model=EspaceOut, status_code=status.HTTP_201_CREATED)
def add_membre(
    espace_id: str,
    payload: MembreIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
    db.execute(
        "INSERT INTO collab_espace_membre (espace_id, utilisateur_id, role) VALUES (%s, %s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO UPDATE SET role = EXCLUDED.role",
        (espace_id, payload.membre_id, payload.role),
        role=user.role,
    )
    return _espace_out(espace_id, user.role)


@router.patch("/espaces/{espace_id}/membres/{membre_id}", response_model=EspaceOut)
def change_role(
    espace_id: str,
    membre_id: str,
    payload: RolePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
    db.execute(
        "UPDATE collab_espace_membre SET role = %s WHERE espace_id = %s AND utilisateur_id = %s",
        (payload.role, espace_id, membre_id),
        role=user.role,
    )
    return _espace_out(espace_id, user.role)


@router.delete("/espaces/{espace_id}/membres/{membre_id}", response_model=EspaceOut)
def remove_membre(
    espace_id: str,
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
    db.execute(
        "DELETE FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, membre_id),
        role=user.role,
    )
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
    db.execute(
        "INSERT INTO collab_demande_acces (espace_id, utilisateur_id) VALUES (%s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO NOTHING",
        (espace_id, user.id),
        role=user.role,
    )
    return RejoindreOut(espace_id=espace_id, espace_nom=e["nom"], statut="demande")


@router.post("/espaces/{espace_id}/demandes", response_model=EspaceOut, status_code=status.HTTP_201_CREATED)
def demander_acces(
    espace_id: str,
    payload: DemandeIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> EspaceOut:
    db.execute(
        "INSERT INTO collab_demande_acces (espace_id, utilisateur_id) VALUES (%s, %s) "
        "ON CONFLICT (espace_id, utilisateur_id) DO NOTHING",
        (espace_id, payload.membre_id),
        role=user.role,
    )
    return _espace_out(espace_id, user.role)


@router.post("/espaces/{espace_id}/demandes/{demande_id}/accepter", response_model=EspaceOut)
def accepter_demande(
    espace_id: str,
    demande_id: str,
    payload: AccepterIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
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
    return _espace_out(espace_id, user.role)


@router.delete("/espaces/{espace_id}/demandes/{demande_id}", response_model=EspaceOut)
def refuser_demande(
    espace_id: str,
    demande_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> EspaceOut:
    require_espace_role(espace_id, user, GERANTS)
    db.execute(
        "DELETE FROM collab_demande_acces WHERE id = %s AND espace_id = %s",
        (demande_id, espace_id),
        role=user.role,
    )
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
