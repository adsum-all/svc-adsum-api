"""Rich cards for the collaboration spaces, served from PostgreSQL.

A card carries labels, assignees and followers, checklists, attachments, a comment
thread (with reactions, mentions and read receipts), an activity feed, a due date,
reminder, priority, complexity and a cover. This module builds the full nested
``CarteProto`` the front expects and exposes the card CRUD, move, duplicate and
archive. Comments, reactions and checklist mutations live in
``collaboration_cartes_social``. Space role is enforced via the shared helpers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db
from .collaboration_espaces import GERANTS, require_espace_role
from .fields import ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-cartes"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")
LECTEURS = ("proprietaire", "admin", "membre", "observateur")
_INCR_COMPTEUR = "UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s"


class ReactionOut(BaseModel):
    membre_id: str
    type: str


class PieceOut(BaseModel):
    id: str
    nom: str
    taille: int
    type: str
    cree_le: str | None = None
    data_url: str | None = None
    couverture: bool = False


class CommentaireProtoOut(BaseModel):
    id: str
    auteur_id: str
    corps: str
    cree_le: str | None = None
    edite_le: str | None = None
    reactions: list[ReactionOut]
    pieces: list[PieceOut]
    lu_par: list[str]
    mentions: list[str]


class ChecklistItemOut(BaseModel):
    id: str
    texte: str
    fait: bool
    assigne_id: str | None = None
    echeance: str | None = None


class ChecklistOut(BaseModel):
    id: str
    titre: str
    items: list[ChecklistItemOut]


class ActiviteOut(BaseModel):
    id: str
    auteur_id: str
    cree_le: str | None = None
    texte: str


class CarteProtoOut(BaseModel):
    id: str
    tableau_id: str
    colonne_id: str
    numero: int
    titre: str
    description: str
    etiquettes: list[str]
    assignes: list[str]
    suiveurs: list[str]
    debut: str | None = None
    echeance: str | None = None
    rappel: str
    priorite: str
    complexite: int | None = None
    position: int
    checklists: list[ChecklistOut]
    pieces: list[PieceOut]
    commentaires: list[CommentaireProtoOut]
    activite: list[ActiviteOut]
    archive: bool
    modele: bool
    couverture_id: str | None = None


class CarteIn(BaseModel):
    tableau_id: ShortStr
    colonne_id: ShortStr
    titre: TitleStr


class CartePatch(BaseModel):
    titre: TitleStr | None = None
    description: TextStr | None = None
    colonne_id: ShortStr | None = None
    priorite: ShortStr | None = None
    complexite: int | None = None
    debut: datetime | None = None
    echeance: datetime | None = None
    rappel: ShortStr | None = None
    position: int | None = None
    couverture_id: ShortStr | None = None
    etiquettes: list[str] | None = None
    assignes: list[str] | None = None
    suiveurs: list[str] | None = None


class MoveIn(BaseModel):
    colonne_id: ShortStr
    position: int


class DeplacerIn(BaseModel):
    tableau_id: ShortStr


class ArchiveIn(BaseModel):
    archive: bool


_CARTE_COLS = (
    "id, tableau_id, colonne_id, numero, titre, description, debut, echeance, rappel, "
    "priorite, complexite, position, archive, modele, couverture_id"
)


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _espace_of_tableau(tableau_id: str, role: str) -> str:
    row = db.fetch_one("SELECT espace_id FROM collab_tableau WHERE id = %s", (tableau_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="board not found")
    return str(row["espace_id"])


def _espace_of_carte(carte_id: str, role: str) -> tuple[str, str]:
    row = db.fetch_one(
        "SELECT c.tableau_id, t.espace_id FROM collab_carte c JOIN collab_tableau t ON t.id = c.tableau_id "
        "WHERE c.id = %s",
        (carte_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    return str(row["tableau_id"]), str(row["espace_id"])


def _pieces_for(where_col: str, ref_id: str, couverture_id: str | None, role: str) -> list[PieceOut]:
    rows = db.fetch_all(
        f"SELECT id, nom, taille, type, url, cree_le FROM collab_piece WHERE {where_col} = %s ORDER BY cree_le",
        (ref_id,),
        role=role,
    )
    return [
        PieceOut(
            id=str(p["id"]),
            nom=p["nom"],
            taille=int(p["taille"]),
            type=p["type"],
            cree_le=_iso(p["cree_le"]),
            data_url=p["url"],
            couverture=(couverture_id is not None and str(p["id"]) == couverture_id),
        )
        for p in rows
    ]


def _commentaires_for(carte_id: str, role: str) -> list[CommentaireProtoOut]:
    rows = db.fetch_all(
        "SELECT id, auteur_id, corps, cree_le, edite_le FROM collab_commentaire WHERE carte_id = %s ORDER BY cree_le",
        (carte_id,),
        role=role,
    )
    out: list[CommentaireProtoOut] = []
    for c in rows:
        cid = str(c["id"])
        reactions = db.fetch_all(
            "SELECT utilisateur_id, type FROM collab_reaction WHERE commentaire_id = %s", (cid,), role=role
        )
        mentions = db.fetch_all(
            "SELECT utilisateur_id FROM collab_commentaire_mention WHERE commentaire_id = %s", (cid,), role=role
        )
        lus = db.fetch_all(
            "SELECT utilisateur_id FROM collab_commentaire_lu WHERE commentaire_id = %s", (cid,), role=role
        )
        out.append(
            CommentaireProtoOut(
                id=cid,
                auteur_id=str(c["auteur_id"]) if c["auteur_id"] else "",
                corps=c["corps"],
                cree_le=_iso(c["cree_le"]),
                edite_le=_iso(c["edite_le"]),
                reactions=[ReactionOut(membre_id=str(r["utilisateur_id"]), type=r["type"]) for r in reactions],
                pieces=_pieces_for("commentaire_id", cid, None, role),
                lu_par=[str(x["utilisateur_id"]) for x in lus],
                mentions=[str(x["utilisateur_id"]) for x in mentions],
            )
        )
    return out


def _checklists_for(carte_id: str, role: str) -> list[ChecklistOut]:
    lists = db.fetch_all(
        "SELECT id, titre FROM collab_checklist WHERE carte_id = %s ORDER BY position", (carte_id,), role=role
    )
    out: list[ChecklistOut] = []
    for cl in lists:
        clid = str(cl["id"])
        items = db.fetch_all(
            "SELECT id, texte, fait, assigne_id, echeance FROM collab_checklist_item "
            "WHERE checklist_id = %s ORDER BY position",
            (clid,),
            role=role,
        )
        out.append(
            ChecklistOut(
                id=clid,
                titre=cl["titre"],
                items=[
                    ChecklistItemOut(
                        id=str(it["id"]),
                        texte=it["texte"],
                        fait=bool(it["fait"]),
                        assigne_id=str(it["assigne_id"]) if it["assigne_id"] else None,
                        echeance=_iso(it["echeance"]),
                    )
                    for it in items
                ],
            )
        )
    return out


def carte_out(row: dict[str, Any], role: str) -> CarteProtoOut:
    """Assemble the full nested card the front expects, from the card row plus its
    labels, members, checklists, attachments, comments and activity."""
    cid = str(row["id"])
    couverture_id = str(row["couverture_id"]) if row["couverture_id"] else None
    etiquettes = db.fetch_all(
        "SELECT etiquette_id FROM collab_carte_etiquette WHERE carte_id = %s", (cid,), role=role
    )
    membres = db.fetch_all(
        "SELECT utilisateur_id, role FROM collab_carte_membre WHERE carte_id = %s", (cid,), role=role
    )
    activite = db.fetch_all(
        "SELECT id, auteur_id, texte, cree_le FROM collab_activite WHERE carte_id = %s ORDER BY cree_le",
        (cid,),
        role=role,
    )
    return CarteProtoOut(
        id=cid,
        tableau_id=str(row["tableau_id"]),
        colonne_id=str(row["colonne_id"]),
        numero=int(row["numero"]) if row["numero"] is not None else 0,
        titre=row["titre"],
        description=row["description"] or "",
        etiquettes=[str(e["etiquette_id"]) for e in etiquettes],
        assignes=[str(m["utilisateur_id"]) for m in membres if m["role"] == "assigne"],
        suiveurs=[str(m["utilisateur_id"]) for m in membres if m["role"] == "suiveur"],
        debut=_iso(row["debut"]),
        echeance=_iso(row["echeance"]),
        rappel=row["rappel"] or "aucun",
        priorite=row["priorite"] or "normale",
        complexite=int(row["complexite"]) if row["complexite"] is not None else None,
        position=int(row["position"]),
        checklists=_checklists_for(cid, role),
        pieces=_pieces_for("carte_id", cid, couverture_id, role),
        commentaires=_commentaires_for(cid, role),
        activite=[
            ActiviteOut(
                id=str(a["id"]),
                auteur_id=str(a["auteur_id"]) if a["auteur_id"] else "",
                cree_le=_iso(a["cree_le"]),
                texte=a["texte"],
            )
            for a in activite
        ],
        archive=bool(row["archive"]),
        modele=bool(row["modele"]),
        couverture_id=couverture_id,
    )


def _fetch_carte(carte_id: str, role: str) -> CarteProtoOut:
    row = db.fetch_one(f"SELECT {_CARTE_COLS} FROM collab_carte WHERE id = %s", (carte_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    return carte_out(row, role)


@router.get("/tableaux/{tableau_id}/cartes", response_model=list[CarteProtoOut])
def list_cartes(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteProtoOut]:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, LECTEURS)
    rows = db.fetch_all(
        f"SELECT {_CARTE_COLS} FROM collab_carte WHERE tableau_id = %s AND NOT archive ORDER BY position",
        (tableau_id,),
        role=user.role,
    )
    return [carte_out(r, user.role) for r in rows]


@router.get("/tableaux/{tableau_id}/cartes-archivees", response_model=list[CarteProtoOut])
def list_cartes_archivees(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteProtoOut]:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, LECTEURS)
    rows = db.fetch_all(
        f"SELECT {_CARTE_COLS} FROM collab_carte WHERE tableau_id = %s AND archive ORDER BY position",
        (tableau_id,),
        role=user.role,
    )
    return [carte_out(r, user.role) for r in rows]


@router.post("/cartes-espace", response_model=CarteProtoOut, status_code=status.HTTP_201_CREATED)
def create_carte(
    payload: CarteIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteProtoOut:
    espace_id = _espace_of_tableau(payload.tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    created = db.execute(
        "INSERT INTO collab_carte (tableau_id, colonne_id, titre, position, numero, cree_par) "
        "VALUES (%s, %s, %s, "
        "(SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
        "(SELECT coalesce(max(numero), 0) + 1 FROM collab_carte WHERE tableau_id = %s), %s) "
        "RETURNING id",
        (payload.tableau_id, payload.colonne_id, payload.titre, payload.colonne_id, payload.tableau_id, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not created")
    cid = str(created["id"])
    db.execute(
        "INSERT INTO collab_carte_membre (carte_id, utilisateur_id, role) VALUES (%s, %s, 'suiveur') "
        "ON CONFLICT DO NOTHING",
        (cid, user.id),
        role=user.role,
    )
    db.execute(
        "INSERT INTO collab_activite (carte_id, auteur_id, texte) VALUES (%s, %s, %s)",
        (cid, user.id, f"a cree la carte (# {payload.titre})"),
        role=user.role,
    )
    db.execute(
        "UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s",
        (payload.tableau_id,),
        role=user.role,
    )
    return _fetch_carte(cid, user.role)


def _sync_carte_relations(cid: str, fields: dict[str, Any], user: UserMe) -> None:
    if "etiquettes" in fields:
        db.execute("DELETE FROM collab_carte_etiquette WHERE carte_id = %s", (cid,), role=user.role)
        for eid in fields.pop("etiquettes") or []:
            db.execute(
                "INSERT INTO collab_carte_etiquette (carte_id, etiquette_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (cid, eid),
                role=user.role,
            )
    for champ, role_val in (("assignes", "assigne"), ("suiveurs", "suiveur")):
        if champ in fields:
            db.execute(
                "DELETE FROM collab_carte_membre WHERE carte_id = %s AND role = %s", (cid, role_val), role=user.role
            )
            for uid in fields.pop(champ) or []:
                db.execute(
                    "INSERT INTO collab_carte_membre (carte_id, utilisateur_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (cid, uid, role_val),
                    role=user.role,
                )


@router.patch("/cartes-espace/{carte_id}", response_model=CarteProtoOut)
def update_carte(
    carte_id: str,
    payload: CartePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    fields = payload.model_dump(exclude_unset=True)
    _sync_carte_relations(carte_id, fields, user)
    if fields:
        sets = ", ".join(f"{key} = %s" for key in fields)
        db.execute(
            f"UPDATE collab_carte SET {sets}, maj_le = now() WHERE id = %s",
            (*fields.values(), carte_id),
            role=user.role,
        )
    return _fetch_carte(carte_id, user.role)


@router.post("/cartes-espace/{carte_id}/deplacer", response_model=CarteProtoOut)
def move_carte(
    carte_id: str,
    payload: MoveIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    db.execute(
        "UPDATE collab_carte SET colonne_id = %s, position = %s, maj_le = now() WHERE id = %s",
        (payload.colonne_id, payload.position, carte_id),
        role=user.role,
    )
    return _fetch_carte(carte_id, user.role)


@router.post("/cartes-espace/{carte_id}/deplacer-tableau", response_model=CarteProtoOut)
def deplacer_vers_tableau(
    carte_id: str,
    payload: DeplacerIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    espace_cible = _espace_of_tableau(payload.tableau_id, user.role)
    require_espace_role(espace_cible, user, MEMBRES_ACTIFS)
    first_col = db.fetch_one(
        "SELECT id FROM collab_colonne WHERE tableau_id = %s AND NOT archivee ORDER BY position LIMIT 1",
        (payload.tableau_id,),
        role=user.role,
    )
    if not first_col:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="target board has no column")
    db.execute(
        "UPDATE collab_carte SET tableau_id = %s, colonne_id = %s, maj_le = now() WHERE id = %s",
        (payload.tableau_id, str(first_col["id"]), carte_id),
        role=user.role,
    )
    db.execute(_INCR_COMPTEUR, (payload.tableau_id,), role=user.role)
    return _fetch_carte(carte_id, user.role)


@router.post("/cartes-espace/{carte_id}/dupliquer", response_model=CarteProtoOut, status_code=status.HTTP_201_CREATED)
def dupliquer_carte(
    carte_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteProtoOut:
    tableau_id, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    src = db.fetch_one(
        "SELECT colonne_id, titre, description, priorite, complexite, rappel FROM collab_carte WHERE id = %s",
        (carte_id,),
        role=user.role,
    )
    if not src:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    created = db.execute(
        "INSERT INTO collab_carte (tableau_id, colonne_id, titre, description, priorite, complexite, rappel, "
        "position, numero, cree_par) VALUES (%s, %s, %s, %s, %s, %s, %s, "
        "(SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
        "(SELECT coalesce(max(numero), 0) + 1 FROM collab_carte WHERE tableau_id = %s), %s) RETURNING id",
        (
            tableau_id, src["colonne_id"], f"{src['titre']} (copie)", src["description"], src["priorite"],
            src["complexite"], src["rappel"], src["colonne_id"], tableau_id, user.id,
        ),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not duplicated")
    db.execute(_INCR_COMPTEUR, (tableau_id,), role=user.role)
    return _fetch_carte(str(created["id"]), user.role)


@router.post("/cartes-espace/{carte_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_carte(
    carte_id: str,
    payload: ArchiveIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    db.execute(
        "UPDATE collab_carte SET archive = %s, maj_le = now() WHERE id = %s",
        (payload.archive, carte_id),
        role=user.role,
    )


@router.delete("/cartes-espace/{carte_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_carte(
    carte_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> None:
    tableau_id, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, GERANTS)
    db.execute("DELETE FROM collab_carte WHERE id = %s", (carte_id,), role=user.role)
    db.execute(
        "UPDATE collab_tableau SET compteur_cartes = greatest(compteur_cartes - 1, 0) WHERE id = %s",
        (tableau_id,),
        role=user.role,
    )
