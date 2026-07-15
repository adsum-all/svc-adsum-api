"""Space-scoped boards (tableaux) and their columns, served from PostgreSQL.

The rich board layer of the collaboration prototype: a board belongs to a space,
carries visibility (space-wide or private participants), a favourite flag and a
card counter; columns carry colour, collapse, WIP limit and archive. Backs the
``lib/store`` tableaux and column contract. Space membership and role are enforced
via the shared space helpers; every query runs under the caller role (RLS).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .collaboration_espaces import (
    GERANTS,
    _initials,
    _name_from_email,
    require_espace_role,
)
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-tableaux"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")


def _col(nom: str, couleur: str | None = None, wip: int | None = None) -> dict[str, Any]:
    return {"nom": nom, "couleur": couleur, "wip": wip}


# Premium, enterprise-grade board templates (original content). Each column carries
# a colour and an optional work-in-progress limit, applied at creation time so a
# board from a template looks and behaves like the promise of the picker.
_SLATE, _BLUE, _AMBER, _VIOLET, _GREEN, _RED, _TEAL = (
    "#94a3b8", "#3b82f6", "#f59e0b", "#8b5cf6", "#22c55e", "#ef4444", "#14b8a6"
)
MODELES: dict[str, dict[str, Any]] = {
    "vide": {
        "libelle": "Tableau vierge",
        "description": "Trois colonnes simples pour démarrer de zéro.",
        "colonnes": [_col("À faire"), _col("En cours"), _col("Terminé", _GREEN)],
    },
    "kanban": {
        "libelle": "Kanban simple",
        "description": "Flux de travail visuel avec revue et limite d'en-cours.",
        "colonnes": [_col("Backlog", _SLATE), _col("À faire", _BLUE), _col("En cours", _AMBER, 3),
                     _col("En revue", _VIOLET), _col("Terminé", _GREEN)],
    },
    "projet": {
        "libelle": "Gestion de projet",
        "description": "Du cadrage à la livraison, pour piloter un projet complet.",
        "colonnes": [_col("Idées", _SLATE), _col("Cadrage", _BLUE), _col("En cours", _AMBER, 4),
                     _col("En revue", _VIOLET), _col("Livré", _GREEN)],
    },
    "contenu": {
        "libelle": "Calendrier éditorial",
        "description": "Production de contenu : de l'idée à la publication.",
        "colonnes": [_col("Idées", _SLATE), _col("Rédaction", _BLUE), _col("Relecture", _VIOLET),
                     _col("Programmé", _TEAL), _col("Publié", _GREEN)],
    },
    "evenement": {
        "libelle": "Organisation d'événement",
        "description": "Préparer un événement, de la planification au bilan.",
        "colonnes": [_col("À planifier", _SLATE), _col("Logistique", _BLUE), _col("Confirmé", _TEAL),
                     _col("Jour J", _AMBER), _col("Bilan", _GREEN)],
    },
    "sprint": {
        "libelle": "Sprint agile",
        "description": "Cadence de sprint avec backlog, test et limite d'en-cours.",
        "colonnes": [_col("Backlog produit", _SLATE), _col("Sélectionné", _BLUE), _col("En cours", _AMBER, 5),
                     _col("Test", _VIOLET), _col("Terminé", _GREEN)],
    },
    "support": {
        "libelle": "Suivi des demandes",
        "description": "Traiter les demandes entrantes, avec mise en attente.",
        "colonnes": [_col("Nouveau", _BLUE), _col("En analyse", _VIOLET), _col("En cours", _AMBER, 6),
                     _col("En attente", _RED), _col("Résolu", _GREEN)],
    },
    "recrutement": {
        "libelle": "Recrutement",
        "description": "Pipeline de recrutement, de la candidature à l'intégration.",
        "colonnes": [_col("Candidatures", _SLATE), _col("Présélection", _BLUE), _col("Entretien", _VIOLET),
                     _col("Offre", _AMBER), _col("Intégré", _GREEN)],
    },
    "campagne": {
        "libelle": "Campagne marketing",
        "description": "Piloter une campagne, de l'idée à l'analyse des résultats.",
        "colonnes": [_col("Idées", _SLATE), _col("Préparation", _BLUE), _col("Validation", _VIOLET),
                     _col("Diffusion", _AMBER), _col("Analyse", _GREEN)],
    },
    "objectifs": {
        "libelle": "Objectifs et résultats",
        "description": "Suivre des objectifs, avec repérage des risques.",
        "colonnes": [_col("À définir", _SLATE), _col("En cours", _AMBER, 4), _col("À risque", _RED),
                     _col("Atteint", _GREEN)],
    },
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
    instructions: bool = False


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
        f"SELECT {_TABLEAU_COLS} FROM collab_tableau "
        "WHERE espace_id = %s AND archive = %s AND supprime_le IS NULL ORDER BY cree_le DESC",
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
            if not allowed and not user.acces_technique_global:
                # A private board is visible only to its explicit participants, for
                # everyone: no member-admin bypass. Space membership is necessary but not
                # sufficient here; the board owner must add you as a participant. The sole
                # exception is a technical super-admin (acces_technique_global), a support
                # identity that sees every board to maintain the platform.
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


# --- Board participants (Trello-style sharing of a private board) ------------

class ParticipantOut(BaseModel):
    utilisateur_id: str
    nom: str
    initiales: str
    role_espace: str | None = None


class ParticipantIn(BaseModel):
    utilisateur_id: ShortStr


def _participants(tableau_id: str, role: str) -> list[ParticipantOut]:
    """The people who can see and act on a private board, with display names and
    their role in the space, so the UI can show avatars like Trello's members."""
    espace_id = _espace_of_tableau(tableau_id, role)
    rows = db.fetch_all(
        "SELECT p.utilisateur_id, u.email, m.nom_affiche, "
        "(SELECT em.role FROM collab_espace_membre em "
        "WHERE em.espace_id = %s AND em.utilisateur_id = p.utilisateur_id) AS role_espace "
        "FROM collab_tableau_participant p "
        "JOIN utilisateur u ON u.id = p.utilisateur_id "
        "LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE p.tableau_id = %s ORDER BY u.email",
        (espace_id, tableau_id),
        role=role,
    )
    out: list[ParticipantOut] = []
    for r in rows:
        nom = str(r["nom_affiche"] or _name_from_email(str(r["email"])))
        out.append(ParticipantOut(
            utilisateur_id=str(r["utilisateur_id"]), nom=nom, initiales=_initials(nom),
            role_espace=(str(r["role_espace"]) if r.get("role_espace") else None),
        ))
    return out


@router.get("/tableaux-espace/{tableau_id}/participants", response_model=list[ParticipantOut])
def list_participants(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[ParticipantOut]:
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, ("proprietaire", "admin", "membre", "observateur"))
    return _participants(tableau_id, user.role)


@router.post(
    "/tableaux-espace/{tableau_id}/participants",
    response_model=list[ParticipantOut],
    status_code=status.HTTP_201_CREATED,
)
def add_participant(
    tableau_id: str,
    payload: ParticipantIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> list[ParticipantOut]:
    """Add someone to a private board. Only an active space member manages the
    board's people, and only a member OF THAT SPACE can be added, so a board can
    never be shared with someone outside its space."""
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    cible = str(payload.utilisateur_id)
    est_membre_espace = db.fetch_one(
        "SELECT 1 FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, cible),
        role=user.role,
    )
    if not est_membre_espace:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cette personne doit d'abord etre membre de l'espace avant d'etre ajoutee au tableau.",
        )
    db.execute(
        "INSERT INTO collab_tableau_participant (tableau_id, utilisateur_id) VALUES (%s, %s) "
        "ON CONFLICT (tableau_id, utilisateur_id) DO NOTHING",
        (tableau_id, cible),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_tableau_participant_ajout", "collab_tableau", tableau_id, {"membre": cible})
    return _participants(tableau_id, user.role)


@router.delete("/tableaux-espace/{tableau_id}/participants/{utilisateur_id}", response_model=list[ParticipantOut])
def remove_participant(
    tableau_id: str,
    utilisateur_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> list[ParticipantOut]:
    """Remove someone from a private board. The board creator cannot be removed,
    so a private board never ends up with nobody who can see it."""
    espace_id = _espace_of_tableau(tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    createur = db.fetch_one("SELECT cree_par FROM collab_tableau WHERE id = %s", (tableau_id,), role=user.role)
    if createur and str(createur["cree_par"]) == str(utilisateur_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le createur du tableau ne peut pas etre retire de ses participants.",
        )
    db.execute(
        "DELETE FROM collab_tableau_participant WHERE tableau_id = %s AND utilisateur_id = %s",
        (tableau_id, utilisateur_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_tableau_participant_retrait", "collab_tableau", tableau_id,
              {"membre": utilisateur_id})
    return _participants(tableau_id, user.role)


def _create_board(
    espace_id: str, nom: str, description: str, visibilite: str, colonnes: list[dict[str, Any]], user: UserMe
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
    # Mandatory 'Instructions du canal' column in first position, unless the admin
    # turned the behaviour off in the back office. Channel instructions land here.
    depart = 0
    if _colonne_instructions_auto(user.role):
        db.execute(
            "INSERT INTO collab_colonne (tableau_id, nom, position, instructions) VALUES (%s, %s, 0, true)",
            (tid, "Instructions du canal"), role=user.role,
        )
        depart = 1
    for i, col in enumerate(colonnes):
        db.execute(
            "INSERT INTO collab_colonne (tableau_id, nom, position, couleur, wip) VALUES (%s, %s, %s, %s, %s)",
            (tid, col["nom"], depart + i, col.get("couleur"), col.get("wip")),
            role=user.role,
        )
    return _fetch_tableau(tid, user.role)


def _colonne_instructions_auto(role: str) -> bool:
    """Whether new boards get the mandatory instruction column (back-office toggle,
    default on)."""
    row = db.fetch_one(
        "SELECT valeur FROM parametre WHERE cle = 'collab_colonne_instructions_auto'", (), role=role,
    )
    if not row or row["valeur"] is None:
        return True
    return str(row["valeur"]).strip().lower() not in ("false", '"false"', "0")


@router.post("/tableaux-espace", response_model=TableauOut, status_code=status.HTTP_201_CREATED)
def create_tableau(
    payload: TableauIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> TableauOut:
    require_espace_role(payload.espace_id, user, MEMBRES_ACTIFS)
    return _create_board(
        payload.espace_id, payload.nom, payload.description or "", payload.visibilite, MODELES["vide"]["colonnes"], user
    )


@router.post("/tableaux-espace/depuis-modele", response_model=TableauOut, status_code=status.HTTP_201_CREATED)
def create_tableau_modele(
    payload: ModeleIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> TableauOut:
    require_espace_role(payload.espace_id, user, MEMBRES_ACTIFS)
    modele = MODELES.get(payload.modele, MODELES["vide"])
    return _create_board(payload.espace_id, payload.nom, "", payload.visibilite, modele["colonnes"], user)


@router.get("/modeles-catalogue")
def modeles_catalogue(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> list[dict[str, Any]]:
    """The premium board templates the picker offers, with their columns (name,
    colour, WIP), so the front previews exactly what a template will create."""
    return [
        {
            "id": key,
            "libelle": modele["libelle"],
            "description": modele["description"],
            "colonnes": [
                {"nom": c["nom"], "couleur": c["couleur"], "wip": c["wip"]} for c in modele["colonnes"]
            ],
        }
        for key, modele in MODELES.items()
    ]


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


# Board deletion (trash + deferred definitive delete, RGPD storage sweep) is owned by
# collaboration_corbeille.py (DELETE /tableaux-espace/{id}); it is registered first, so
# a second handler here would be dead code. Kept single-sourced on purpose.


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
        instructions=bool(row.get("instructions")),
    )


_COL_COLS = "id, tableau_id, nom, position, couleur, repliee, wip, archivee, instructions"


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
    # One atomic reindex of EVERY column of the board: listed columns take the client
    # order, omitted ones keep their prior order after them, so positions stay unique
    # 0..N-1 even if the ordre list is partial/duplicated or two gerants race.
    db.execute(
        "UPDATE collab_colonne c SET position = sub.new_pos FROM ("
        "  SELECT id, row_number() OVER ("
        "    ORDER BY array_position(%s::uuid[], id) NULLS LAST, position, id) - 1 AS new_pos"
        "  FROM collab_colonne WHERE tableau_id = %s"
        ") sub WHERE c.id = sub.id AND c.tableau_id = %s",
        (payload.ordre, tableau_id, tableau_id),
        role=user.role,
    )
