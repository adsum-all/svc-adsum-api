"""Committee collaboration space (interface E), served from the real PostgreSQL.

A Trello-like board: columns hold preparation cards, each card carries a
preparation sheet, a comment thread (polled for near real-time) and a version
history. A prepared card can be published: it creates an ``evenement`` and opens
its check-in session. Reserved to the committee roles; direction may read.
Every query runs under the caller role, activating the RLS policies (ADR-0002).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration"])


DEFAULT_COLUMNS = ("A preparer", "En preparation", "Pret", "Publie")


class TableauIn(BaseModel):
    nom: TitleStr
    description: TextStr | None = None


class TableauOut(BaseModel):
    id: str
    nom: str
    description: str | None = None
    cartes_total: int = 0
    cree_le: datetime | None = None


class ColonneOut(BaseModel):
    id: str
    nom: str
    position: int


class CarteOut(BaseModel):
    id: str
    tableau_id: str
    colonne_id: str
    titre: str
    description: str | None = None
    type_activite: str | None = None
    date_prevue: datetime | None = None
    lieu: str | None = None
    position: int
    publie: bool
    evenement_id: str | None = None


class TableauDetail(BaseModel):
    id: str
    nom: str
    description: str | None = None
    colonnes: list[ColonneOut]
    cartes: list[CarteOut]


class CarteIn(BaseModel):
    tableau_id: ShortStr
    colonne_id: ShortStr
    titre: TitleStr
    description: TextStr | None = None
    type_activite: ShortStr | None = None
    date_prevue: datetime | None = None
    lieu: LineStr | None = None


class CartePatch(BaseModel):
    colonne_id: ShortStr | None = None
    titre: TitleStr | None = None
    description: TextStr | None = None
    type_activite: ShortStr | None = None
    date_prevue: datetime | None = None
    lieu: LineStr | None = None
    position: int | None = None


class CommentaireIn(BaseModel):
    corps: TextStr


class CommentaireOut(BaseModel):
    id: str
    auteur_nom: str | None = None
    corps: str
    cree_le: datetime | None = None


class CarteCalendrier(BaseModel):
    id: str
    titre: str
    date_prevue: datetime
    lieu: str | None = None
    type_activite: str | None = None
    tableau_id: str
    tableau_nom: str
    publie: bool
    evenement_id: str | None = None


def _carte_from_row(r: dict[str, object]) -> CarteOut:
    return CarteOut(
        id=str(r["id"]),
        tableau_id=str(r["tableau_id"]),
        colonne_id=str(r["colonne_id"]),
        titre=str(r["titre"]),
        description=r["description"],  # type: ignore[arg-type]
        type_activite=r["type_activite"],  # type: ignore[arg-type]
        date_prevue=r["date_prevue"],  # type: ignore[arg-type]
        lieu=r["lieu"],  # type: ignore[arg-type]
        position=int(r["position"]),  # type: ignore[arg-type]
        publie=bool(r["publie"]),
        evenement_id=str(r["evenement_id"]) if r["evenement_id"] else None,
    )


@router.get("/calendrier", response_model=list[CarteCalendrier])
def calendrier(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
    debut: datetime | None = None,
    fin: datetime | None = None,
) -> list[CarteCalendrier]:
    """Planned activities across the committee boards.

    Every card that carries a planned date (``date_prevue``) is an activity a
    committee member is programming. Returning them here lets the collaboration
    app show a calendar and schedule activities without going through the admin
    back office. Access follows the same committee RLS as the boards.
    """
    clauses = ["c.date_prevue IS NOT NULL", "NOT c.archive", "NOT t.archive"]
    params: list[object] = []
    if debut is not None:
        clauses.append("c.date_prevue >= %s")
        params.append(debut)
    if fin is not None:
        clauses.append("c.date_prevue <= %s")
        params.append(fin)
    where = " AND ".join(clauses)
    rows = db.fetch_all(
        f"""
        SELECT c.id, c.titre, c.date_prevue, c.lieu, c.type_activite,
               c.tableau_id, t.nom AS tableau_nom, c.publie, c.evenement_id
        FROM collab_carte c
        JOIN collab_tableau t ON t.id = c.tableau_id
        WHERE {where}
        ORDER BY c.date_prevue ASC
        """,
        tuple(params),
        role=user.role,
    )
    return [
        CarteCalendrier(
            id=str(r["id"]),
            titre=str(r["titre"]),
            date_prevue=r["date_prevue"],  # type: ignore[arg-type]
            lieu=r["lieu"],  # type: ignore[arg-type]
            type_activite=r["type_activite"],  # type: ignore[arg-type]
            tableau_id=str(r["tableau_id"]),
            tableau_nom=str(r["tableau_nom"]),
            publie=bool(r["publie"]),
            evenement_id=str(r["evenement_id"]) if r["evenement_id"] else None,
        )
        for r in rows
    ]


@router.get("/tableaux", response_model=list[TableauOut])
def list_tableaux(user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]) -> list[TableauOut]:
    rows = db.fetch_all(
        """
        SELECT t.id, t.nom, t.description, t.cree_le,
               (SELECT count(*) FROM collab_carte c WHERE c.tableau_id = t.id) AS cartes_total
        FROM collab_tableau t
        WHERE NOT t.archive
        ORDER BY t.cree_le DESC
        """,
        (),
        role=user.role,
    )
    return [
        TableauOut(
            id=str(r["id"]),
            nom=r["nom"],
            description=r["description"],
            cartes_total=int(r["cartes_total"]),
            cree_le=r["cree_le"],
        )
        for r in rows
    ]


@router.post("/tableaux", response_model=TableauDetail, status_code=status.HTTP_201_CREATED)
def create_tableau(
    payload: TableauIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> TableauDetail:
    created = db.execute(
        "INSERT INTO collab_tableau (nom, description, cree_par) VALUES (%s, %s, %s) RETURNING id",
        (payload.nom, payload.description, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="board not created")
    tableau_id = str(created["id"])
    for position, nom in enumerate(DEFAULT_COLUMNS):
        db.execute(
            "INSERT INTO collab_colonne (tableau_id, nom, position) VALUES (%s, %s, %s)",
            (tableau_id, nom, position),
            role=user.role,
        )
    return _board_detail(tableau_id, user.role)


@router.get("/tableaux/{tableau_id}", response_model=TableauDetail)
def get_tableau(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> TableauDetail:
    return _board_detail(tableau_id, user.role)


def _board_detail(tableau_id: str, role: str) -> TableauDetail:
    board = db.fetch_one(
        "SELECT id, nom, description FROM collab_tableau WHERE id = %s",
        (tableau_id,),
        role=role,
    )
    if not board:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="board not found")
    colonnes = db.fetch_all(
        "SELECT id, nom, position FROM collab_colonne WHERE tableau_id = %s ORDER BY position ASC",
        (tableau_id,),
        role=role,
    )
    cartes = db.fetch_all(
        """
        SELECT id, tableau_id, colonne_id, titre, description, type_activite,
               date_prevue, lieu, position, publie, evenement_id
        FROM collab_carte
        WHERE tableau_id = %s
        ORDER BY position ASC, cree_le ASC
        """,
        (tableau_id,),
        role=role,
    )
    return TableauDetail(
        id=str(board["id"]),
        nom=board["nom"],
        description=board["description"],
        colonnes=[ColonneOut(id=str(c["id"]), nom=c["nom"], position=int(c["position"])) for c in colonnes],
        cartes=[_carte_from_row(c) for c in cartes],
    )


@router.post("/cartes", response_model=CarteOut, status_code=status.HTTP_201_CREATED)
def create_carte(
    payload: CarteIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteOut:
    created = db.execute(
        """
        INSERT INTO collab_carte
            (tableau_id, colonne_id, titre, description, type_activite, date_prevue, lieu, position, cree_par)
        VALUES (%s, %s, %s, %s, %s, %s, %s,
                (SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), %s)
        RETURNING id, tableau_id, colonne_id, titre, description, type_activite,
                  date_prevue, lieu, position, publie, evenement_id
        """,
        (
            payload.tableau_id,
            payload.colonne_id,
            payload.titre,
            payload.description,
            payload.type_activite,
            payload.date_prevue,
            payload.lieu,
            payload.colonne_id,
            user.id,
        ),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not created")
    return _carte_from_row(created)


@router.patch("/cartes/{carte_id}", response_model=CarteOut)
def update_carte(
    carte_id: str, payload: CartePatch, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteOut:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    _snapshot_version(carte_id, user)
    sets = ", ".join(f"{key} = %s" for key in fields)
    params = list(fields.values())
    params.append(carte_id)
    updated = db.execute(
        f"""
        UPDATE collab_carte SET {sets}, maj_le = now() WHERE id = %s
        RETURNING id, tableau_id, colonne_id, titre, description, type_activite,
                  date_prevue, lieu, position, publie, evenement_id
        """,
        tuple(params),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    return _carte_from_row(updated)


def _snapshot_version(carte_id: str, user: UserMe) -> None:
    row = db.fetch_one(
        "SELECT titre, description, type_activite, date_prevue, lieu, colonne_id FROM collab_carte WHERE id = %s",
        (carte_id,),
        role=user.role,
    )
    if not row:
        return
    snapshot = {
        "titre": row["titre"],
        "description": row["description"],
        "type_activite": row["type_activite"],
        "date_prevue": row["date_prevue"].isoformat() if row["date_prevue"] else None,
        "lieu": row["lieu"],
        "colonne_id": str(row["colonne_id"]),
    }
    db.execute(
        "INSERT INTO collab_version (carte_id, snapshot, auteur_id, auteur_nom) VALUES (%s, %s::jsonb, %s, %s)",
        (carte_id, json.dumps(snapshot), user.id, user.email),
        role=user.role,
    )


@router.get("/cartes/{carte_id}/commentaires", response_model=list[CommentaireOut])
def list_commentaires(
    carte_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CommentaireOut]:
    rows = db.fetch_all(
        "SELECT id, auteur_nom, corps, cree_le FROM collab_commentaire WHERE carte_id = %s ORDER BY cree_le ASC",
        (carte_id,),
        role=user.role,
    )
    return [
        CommentaireOut(id=str(r["id"]), auteur_nom=r["auteur_nom"], corps=r["corps"], cree_le=r["cree_le"])
        for r in rows
    ]


@router.post("/cartes/{carte_id}/commentaires", response_model=CommentaireOut, status_code=status.HTTP_201_CREATED)
def add_commentaire(
    carte_id: str, payload: CommentaireIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CommentaireOut:
    created = db.execute(
        """
        INSERT INTO collab_commentaire (carte_id, auteur_id, auteur_nom, corps)
        VALUES (%s, %s, %s, %s)
        RETURNING id, auteur_nom, corps, cree_le
        """,
        (carte_id, user.id, user.email, payload.corps),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="comment not created")
    return CommentaireOut(
        id=str(created["id"]), auteur_nom=created["auteur_nom"], corps=created["corps"], cree_le=created["cree_le"]
    )


@router.post("/cartes/{carte_id}/publier", response_model=CarteOut)
def publier_carte(
    carte_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteOut:
    carte = db.fetch_one(
        "SELECT id, tableau_id, titre, type_activite, date_prevue, lieu, publie FROM collab_carte WHERE id = %s",
        (carte_id,),
        role=user.role,
    )
    if not carte:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    if carte["publie"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="card already published")
    debut = carte["date_prevue"] or datetime.now(tz=UTC)
    evenement = db.execute(
        """
        INSERT INTO evenement (titre, type, volet, debut, lieu, session_ouverte, ouvert_le, cree_par)
        VALUES (%s, %s, 'A', %s, %s, true, now(), %s)
        RETURNING id
        """,
        (carte["titre"], carte["type_activite"], debut, carte["lieu"], user.id),
        role=user.role,
    )
    if not evenement:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="event not created")
    # The target column is no longer coupled to the exact name 'Publie' (columns
    # are renameable): prefer a column named publie/publie (case and accent
    # insensitive), otherwise fall back to the last column, so a published card
    # always lands in a sensible end column even after a rename.
    publie_col = db.fetch_one(
        "SELECT id FROM collab_colonne WHERE tableau_id = %s "
        "ORDER BY (lower(nom) IN ('publie', 'publié')) DESC, position DESC LIMIT 1",
        (str(carte["tableau_id"]),),
        role=user.role,
    )
    updated = db.execute(
        """
        UPDATE collab_carte
        SET publie = true, evenement_id = %s, colonne_id = coalesce(%s, colonne_id), maj_le = now()
        WHERE id = %s
        RETURNING id, tableau_id, colonne_id, titre, description, type_activite,
                  date_prevue, lieu, position, publie, evenement_id
        """,
        (str(evenement["id"]), str(publie_col["id"]) if publie_col else None, carte_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not updated")
    return _carte_from_row(updated)
