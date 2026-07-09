"""Space-scoped boards (tableaux) and their columns, served from PostgreSQL.

The rich board layer of the collaboration prototype: a board belongs to a space,
carries visibility (space-wide or private participants), a favourite flag and a
card counter; columns carry colour, collapse, WIP limit and archive. Backs the
``lib/store`` tableaux and column contract. Space membership and role are enforced
via the shared space helpers; every query runs under the caller role (RLS).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db
from .collaboration_espaces import GERANTS, require_espace_role
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-tableaux"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")
MODELE_COLONNES: dict[str, tuple[str, ...]] = {
    "vide": ("A faire", "En cours", "Termine"),
    "activite": ("Preparation", "En cours", "Pret", "Publie"),
    "suivi": ("A faire", "En cours", "Bloque", "Termine"),
    "sprint": ("Backlog", "A faire", "En cours", "Revue", "Termine"),
}


class ColonneOut(BaseModel):
    id: str
    tableau_id: str
    nom: str
    position: int
    couleur: str | None = None
    repliee: bool = False
    wip: int | None = None
    archivee: bool = False


class TableauOut(BaseModel):
    id: str
    espace_id: str
    nom: str
    description: str
    visibilite: str
    participants: list[str]
    favori: bool
    archive: bool
    modele: bool
    compteur_cartes: int
    cree_le: str | None = None


class TableauIn(BaseModel):
    espace_id: ShortStr
    nom: TitleStr
    description: TextStr | None = ""
    visibilite: ShortStr = "espace"


class ModeleIn(BaseModel):
    espace_id: ShortStr
    nom: TitleStr
    modele: ShortStr = "vide"
    visibilite: ShortStr = "espace"


class TableauPatch(BaseModel):
    nom: TitleStr | None = None
    description: TextStr | None = None
    visibilite: ShortStr | None = None
    favori: bool | None = None
    archive: bool | None = None


class ArchiveIn(BaseModel):
    archive: bool


class ColonneIn(BaseModel):
    nom: LineStr


class ColonnePatch(BaseModel):
    nom: LineStr | None = None
    couleur: ShortStr | None = None
    repliee: bool | None = None
    wip: int | None = None
    archivee: bool | None = None


class OrdreColonnesIn(BaseModel):
    ordre: list[str]


def _espace_of_tableau(tableau_id: str, role: str) -> str:
    row = db.fetch_one("SELECT espace_id FROM collab_tableau WHERE id = %s", (tableau_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="board not found")
    return str(row["espace_id"])


def _tableau_out(row: dict[str, object], role: str) -> TableauOut:
    tid = str(row["id"])
    parts = db.fetch_all(
        "SELECT utilisateur_id FROM collab_tableau_participant WHERE tableau_id = %s", (tid,), role=role
    )
    return TableauOut(
        id=tid,
        espace_id=str(row["espace_id"]),
        nom=str(row["nom"]),
        description=(row["description"] or "") if isinstance(row["description"], str) else "",
        visibilite=str(row["visibilite"]),
        participants=[str(p["utilisateur_id"]) for p in parts],
        favori=bool(row["favori"]),
        archive=bool(row["archive"]),
        modele=bool(row["modele"]),
        compteur_cartes=int(row["compteur_cartes"]),  # type: ignore[arg-type]
        cree_le=row["cree_le"].isoformat() if row["cree_le"] else None,  # type: ignore[union-attr]
    )


_TABLEAU_COLS = (
    "id, espace_id, nom, description, visibilite, favori, archive, modele, compteur_cartes, cree_le"
)


def _fetch_tableau(tableau_id: str, role: str) -> TableauOut:
    row = db.fetch_one(f"SELECT {_TABLEAU_COLS} FROM collab_tableau WHERE id = %s", (tableau_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="board not found")
    return _tableau_out(row, role)


def _visible_boards(espace_id: str, user: UserMe, archived: bool) -> list[TableauOut]:
    rows = db.fetch_all(
        f"SELECT {_TABLEAU_COLS} FROM collab_tableau WHERE espace_id = %s AND archive = %s ORDER BY cree_le DESC",
        (espace_id, archived),
        role=user.role,
    )
    out: list[TableauOut] = []
    for r in rows:
        if r["visibilite"] == "prive":
            allowed = db.fetch_one(
                "SELECT 1 FROM collab_tableau_participant WHERE tableau_id = %s AND utilisateur_id = %s",
                (str(r["id"]), user.id),
                role=user.role,
            )
            if not allowed and user.role not in ("super_admin", "admin"):
                continue
        out.append(_tableau_out(r, user.role))
    return out


@router.get("/espaces/{espace_id}/tableaux", response_model=list[TableauOut])
def list_tableaux(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[TableauOut]:
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return _visible_boards(espace_id, user, archived=False)


@router.get("/espaces/{espace_id}/tableaux-archives", response_model=list[TableauOut])
def list_tableaux_archives(
    espace_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[TableauOut]:
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return _visible_boards(espace_id, user, archived=True)


@router.get("/tableaux-espace/{tableau_id}", response_model=TableauOut)
def get_tableau(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> TableauOut:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return _fetch_tableau(tableau_id, user.role)


def _create_board(
    espace_id: str, nom: str, description: str, visibilite: str, colonnes: tuple[str, ...], user: UserMe
) -> TableauOut:
    created = db.execute(
        "INSERT INTO collab_tableau (espace_id, nom, description, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (espace_id, nom, description, visibilite, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="board not created")
    tid = str(created["id"])
    if visibilite == "prive":
        db.execute(
            "INSERT INTO collab_tableau_participant (tableau_id, utilisateur_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (tid, user.id),
            role=user.role,
        )
    for position, col in enumerate(colonnes):
        db.execute(
            "INSERT INTO collab_colonne (tableau_id, nom, position) VALUES (%s, %s, %s)",
            (tid, col, position),
            role=user.role,
        )
    return _fetch_tableau(tid, user.role)


@router.post("/tableaux-espace", response_model=TableauOut, status_code=status.HTTP_201_CREATED)
def create_tableau(
    payload: TableauIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> TableauOut:
    require_espace_role(payload.espace_id, user, MEMBRES_ACTIFS)
    return _create_board(
        payload.espace_id, payload.nom, payload.description or "", payload.visibilite, MODELE_COLONNES["vide"], user
    )


@router.post("/tableaux-espace/depuis-modele", response_model=TableauOut, status_code=status.HTTP_201_CREATED)
def create_tableau_modele(
    payload: ModeleIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> TableauOut:
    require_espace_role(payload.espace_id, user, MEMBRES_ACTIFS)
    colonnes = MODELE_COLONNES.get(payload.modele, MODELE_COLONNES["vide"])
    return _create_board(payload.espace_id, payload.nom, "", payload.visibilite, colonnes, user)


@router.patch("/tableaux-espace/{tableau_id}", response_model=TableauOut)
def update_tableau(
    tableau_id: str,
    payload: TableauPatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> TableauOut:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return _fetch_tableau(tableau_id, user.role)
    sets = ", ".join(f"{key} = %s" for key in fields)
    updated = db.execute(
        f"UPDATE collab_tableau SET {sets} WHERE id = %s RETURNING id",
        (*fields.values(), tableau_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="board not found")
    return _fetch_tableau(tableau_id, user.role)


@router.post("/tableaux-espace/{tableau_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_tableau(
    tableau_id: str,
    payload: ArchiveIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, GERANTS)
    db.execute("UPDATE collab_tableau SET archive = %s WHERE id = %s", (payload.archive, tableau_id), role=user.role)


@router.delete("/tableaux-espace/{tableau_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tableau(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> None:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, GERANTS)
    db.execute("DELETE FROM collab_tableau WHERE id = %s", (tableau_id,), role=user.role)


# ---- Columns ----

def _colonne_out(row: dict[str, object]) -> ColonneOut:
    return ColonneOut(
        id=str(row["id"]),
        tableau_id=str(row["tableau_id"]),
        nom=str(row["nom"]),
        position=int(row["position"]),  # type: ignore[arg-type]
        couleur=row["couleur"] if isinstance(row["couleur"], str) else None,
        repliee=bool(row["repliee"]),
        wip=int(row["wip"]) if row["wip"] is not None else None,  # type: ignore[arg-type]
        archivee=bool(row["archivee"]),
    )


_COL_COLS = "id, tableau_id, nom, position, couleur, repliee, wip, archivee"


@router.get("/tableaux/{tableau_id}/colonnes", response_model=list[ColonneOut])
def list_colonnes(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[ColonneOut]:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    rows = db.fetch_all(
        f"SELECT {_COL_COLS} FROM collab_colonne WHERE tableau_id = %s AND NOT archivee ORDER BY position",
        (tableau_id,),
        role=user.role,
    )
    return [_colonne_out(r) for r in rows]


@router.post("/tableaux/{tableau_id}/colonnes", response_model=ColonneOut, status_code=status.HTTP_201_CREATED)
def create_colonne(
    tableau_id: str,
    payload: ColonneIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> ColonneOut:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, GERANTS)
    created = db.execute(
        f"INSERT INTO collab_colonne (tableau_id, nom, position) "
        f"VALUES (%s, %s, (SELECT coalesce(max(position), -1) + 1 FROM collab_colonne WHERE tableau_id = %s)) "
        f"RETURNING {_COL_COLS}",
        (tableau_id, payload.nom, tableau_id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="column not created")
    return _colonne_out(created)


@router.patch("/colonnes/{colonne_id}", response_model=ColonneOut)
def update_colonne(
    colonne_id: str,
    payload: ColonnePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> ColonneOut:
    row = db.fetch_one("SELECT tableau_id FROM collab_colonne WHERE id = %s", (colonne_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="column not found")
    espace_id = _espace_of_tableau(str(row["tableau_id"]), user.role)
    require_espace_role(espace_id, user, GERANTS)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    sets = ", ".join(f"{key} = %s" for key in fields)
    updated = db.execute(
        f"UPDATE collab_colonne SET {sets} WHERE id = %s RETURNING {_COL_COLS}",
        (*fields.values(), colonne_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="column not found")
    return _colonne_out(updated)


@router.delete("/colonnes/{colonne_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_colonne(
    colonne_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> None:
    row = db.fetch_one("SELECT tableau_id FROM collab_colonne WHERE id = %s", (colonne_id,), role=user.role)
    if not row:
        return
    espace_id = _espace_of_tableau(str(row["tableau_id"]), user.role)
    require_espace_role(espace_id, user, GERANTS)
    db.execute("DELETE FROM collab_colonne WHERE id = %s", (colonne_id,), role=user.role)


@router.post("/tableaux/{tableau_id}/colonnes/ordre", status_code=status.HTTP_204_NO_CONTENT)
def reordonner_colonnes(
    tableau_id: str,
    payload: OrdreColonnesIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, GERANTS)
    for index, colonne_id in enumerate(payload.ordre):
        db.execute(
            "UPDATE collab_colonne SET position = %s WHERE id = %s AND tableau_id = %s",
            (index, colonne_id, tableau_id),
            role=user.role,
        )
