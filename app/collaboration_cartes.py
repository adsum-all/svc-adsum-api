"""Rich cards for the collaboration spaces, served from PostgreSQL.

A card carries labels, assignees and followers, checklists, attachments, a comment
thread (with reactions, mentions and read receipts), an activity feed, a due date,
reminder, priority, complexity and a cover. This module builds the full nested
``CarteProto`` the front expects and exposes the card CRUD, move, duplicate and
archive. Comments, reactions and checklist mutations live in
``collaboration_cartes_social``. Space role is enforced via the shared helpers.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import activites, attachments_core, audit, collaboration_notif, db, storage
from .collaboration_cartes_read import _CARTE_COLS, CarteProtoOut, assemble_cartes, carte_out
from .collaboration_cartes_read import CommentaireProtoOut as CommentaireProtoOut  # re-export
from .collaboration_cartes_read import PieceOut as PieceOut  # re-export
from .collaboration_espaces import GERANTS, require_espace_role
from .collaboration_tableaux import require_tableau_visible
from .fields import LineStr, ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .sanitize import sanitize_html, text_content
from .schemas import UserMe

_MENTION_RE = re.compile(r"@([\w.\-]{2,40})")


def _resolve_mentions_espace(texte: str, espace_id: str, role: str) -> list[str]:
    """Match @tokens in text (comment or description HTML) against the space
    members' display name or e-mail local part; return the matched account ids."""
    tokens = {t.lower() for t in _MENTION_RE.findall(texte or "")}
    if not tokens:
        return []
    rows = db.fetch_all(
        "SELECT u.id, u.email, m.nom_affiche FROM collab_espace_membre em "
        "JOIN utilisateur u ON u.id = em.utilisateur_id LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE em.espace_id = %s",
        (espace_id,),
        role=role,
    )
    matched: list[str] = []
    for r in rows:
        local = (r["email"].split("@")[0] or "").lower()
        nom = (r["nom_affiche"] or "").lower().replace(" ", "")
        # Exact handle match only: a prefix like @ma must NOT notify every member whose
        # name/e-mail begins with it (over-notification on paid channels, harassment).
        if any(tok in (local, nom) for tok in tokens):
            matched.append(str(r["id"]))
    return matched

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-cartes"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")
LECTEURS = ("proprietaire", "admin", "membre", "observateur")
_INCR_COMPTEUR = "UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s"


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


def _ctx_carte(carte_id: str, role: str) -> dict[str, Any]:
    """Card title, space name and space id, for notification context."""
    row = db.fetch_one(
        "SELECT c.titre, t.espace_id, e.nom AS espace FROM collab_carte c "
        "JOIN collab_tableau t ON t.id = c.tableau_id JOIN collab_espace e ON e.id = t.espace_id "
        "WHERE c.id = %s",
        (carte_id,),
        role=role,
    )
    if not row:
        return {"titre": "", "espace": "", "espace_id": None}
    return {"titre": row["titre"], "espace": row["espace"] or "", "espace_id": str(row["espace_id"])}


def _fetch_carte(carte_id: str, role: str) -> CarteProtoOut:
    row = db.fetch_one(f"SELECT {_CARTE_COLS} FROM collab_carte WHERE id = %s", (carte_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    return carte_out(row, role)


@router.get("/tableaux/{tableau_id}/cartes", response_model=list[CarteProtoOut])
def list_cartes(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteProtoOut]:
    # A private board's cards are readable only by its participants: enforce board
    # visibility here, not only the space role (space membership alone is not enough).
    require_tableau_visible(tableau_id, user)
    rows = db.fetch_all(
        f"SELECT {_CARTE_COLS} FROM collab_carte WHERE tableau_id = %s AND NOT archive ORDER BY position",
        (tableau_id,),
        role=user.role,
    )
    return assemble_cartes(rows, user.role)


@router.get("/tableaux/{tableau_id}/cartes-archivees", response_model=list[CarteProtoOut])
def list_cartes_archivees(
    tableau_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> list[CarteProtoOut]:
    require_tableau_visible(tableau_id, user)
    rows = db.fetch_all(
        f"SELECT {_CARTE_COLS} FROM collab_carte WHERE tableau_id = %s AND archive ORDER BY position",
        (tableau_id,),
        role=user.role,
    )
    return assemble_cartes(rows, user.role)


def _valider_colonne(colonne_id: str, tableau_id: str, role: str) -> None:
    """Guarantee a column belongs to the given board, so a card can never be created
    or moved into a column of another board (which would make it unreachable)."""
    if not db.fetch_one(
        "SELECT 1 FROM collab_colonne WHERE id = %s AND tableau_id = %s", (colonne_id, tableau_id), role=role
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="colonne invalide pour ce tableau")


@router.post("/cartes-espace", response_model=CarteProtoOut, status_code=status.HTTP_201_CREATED)
def create_carte(
    payload: CarteIn, user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))]
) -> CarteProtoOut:
    espace_id = _espace_of_tableau(payload.tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    _valider_colonne(payload.colonne_id, payload.tableau_id, user.role)
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
    membres_espace: set[str] | None = None
    for champ, role_val in (("assignes", "assigne"), ("suiveurs", "suiveur")):
        if champ in fields:
            nouveaux = [str(u) for u in (fields.pop(champ) or [])]
            # Only real members of the card's space may be assigned or set as followers
            # (like board participants and checklist assignees), never an arbitrary user.
            if nouveaux:
                if membres_espace is None:
                    _, espace_carte = _espace_of_carte(cid, user.role)
                    membres_espace = {
                        str(r["utilisateur_id"]) for r in db.fetch_all(
                            "SELECT utilisateur_id FROM collab_espace_membre WHERE espace_id = %s",
                            (espace_carte,), role=user.role,
                        )
                    }
                if any(u not in membres_espace for u in nouveaux):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="assigné ou suiveur non membre de l'espace",
                    )
            avant = {
                str(r["utilisateur_id"])
                for r in db.fetch_all(
                    "SELECT utilisateur_id FROM collab_carte_membre WHERE carte_id = %s AND role = %s",
                    (cid, role_val),
                    role=user.role,
                )
            }
            db.execute(
                "DELETE FROM collab_carte_membre WHERE carte_id = %s AND role = %s", (cid, role_val), role=user.role
            )
            for uid in nouveaux:
                db.execute(
                    "INSERT INTO collab_carte_membre (carte_id, utilisateur_id, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (cid, uid, role_val),
                    role=user.role,
                )
            # Notify the newly assigned members (in-app + real channels).
            if role_val == "assigne":
                ctx = _ctx_carte(cid, user.role)
                for uid in nouveaux:
                    if uid not in avant and uid != user.id:
                        collaboration_notif.emettre(
                            uid, "assignation", "collab_assignation",
                            f"La carte « {ctx['titre']} » vous a ete assignee", cid, ctx["espace_id"], ctx, user.role,
                        )


@router.patch("/cartes-espace/{carte_id}", response_model=CarteProtoOut)
def update_carte(
    carte_id: str,
    payload: CartePatch,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    tableau_id, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("colonne_id"):
        _valider_colonne(fields["colonne_id"], tableau_id, user.role)
    description_maj = "description" in fields
    ancienne_desc = ""
    if description_maj:
        # Snapshot the old description so only newly-added mentions are notified.
        prev = db.fetch_one("SELECT description FROM collab_carte WHERE id = %s", (carte_id,), role=user.role)
        ancienne_desc = (prev["description"] if prev else "") or ""
        # The description is authored as rich HTML: sanitise it (allowlist) so nothing
        # executable is stored, then render it back through the same allowlist.
        fields["description"] = sanitize_html(fields["description"]) or ""
    _sync_carte_relations(carte_id, fields, user)
    if fields:
        sets = ", ".join(f"{key} = %s" for key in fields)
        db.execute(
            f"UPDATE collab_carte SET {sets}, maj_le = now() WHERE id = %s",
            (*fields.values(), carte_id),
            role=user.role,
        )
    if description_maj and fields.get("description"):
        # Tagging in the description notifies mentioned space members (in-app + real
        # channels). Mentions are read from visible text only (not attribute values),
        # and only members newly mentioned since the previous save are notified, with
        # dedup so overlapping edits never fan out twice.
        titre_row = db.fetch_one("SELECT titre FROM collab_carte WHERE id = %s", (carte_id,), role=user.role)
        titre = titre_row["titre"] if titre_row else ""
        ctx = {"titre": titre, "espace": collaboration_notif.nom_espace(espace_id, user.role)}
        deja = set(_resolve_mentions_espace(text_content(ancienne_desc), espace_id, user.role))
        for mid in _resolve_mentions_espace(text_content(fields["description"]), espace_id, user.role):
            if mid != user.id and mid not in deja:
                collaboration_notif.emettre(
                    mid, "mention", "collab_mention", f"Vous etes mentionne dans « {titre} »",
                    carte_id, espace_id, ctx, user.role, dedup=True,
                )
    return _fetch_carte(carte_id, user.role)


@router.post("/cartes-espace/{carte_id}/deplacer", response_model=CarteProtoOut)
def move_carte(
    carte_id: str,
    payload: MoveIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    tableau_id, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    _valider_colonne(payload.colonne_id, tableau_id, user.role)
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
    source_tableau, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    espace_cible = _espace_of_tableau(payload.tableau_id, user.role)
    require_espace_role(espace_cible, user, MEMBRES_ACTIFS)
    if source_tableau == payload.tableau_id:
        return _fetch_carte(carte_id, user.role)
    first_col = db.fetch_one(
        "SELECT id FROM collab_colonne WHERE tableau_id = %s AND NOT archivee ORDER BY position LIMIT 1",
        (payload.tableau_id,),
        role=user.role,
    )
    if not first_col:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="target board has no column")
    col_id = str(first_col["id"])
    # Append at the end of the target column (reset the stale source position), move the
    # card, then keep both boards' card counters exact: +1 on the target, -1 on the source.
    db.execute(
        "UPDATE collab_carte SET tableau_id = %s, colonne_id = %s, "
        "position = (SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
        "maj_le = now() WHERE id = %s",
        (payload.tableau_id, col_id, col_id, carte_id),
        role=user.role,
    )
    db.execute(_INCR_COMPTEUR, (payload.tableau_id,), role=user.role)
    db.execute(
        "UPDATE collab_tableau SET compteur_cartes = greatest(compteur_cartes - 1, 0) WHERE id = %s",
        (source_tableau,), role=user.role,
    )
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
    # The DB cascade removes the card's pieces and its comments' pieces, but not the
    # storage objects. Sweep them first (RGPD erasure), before the rows disappear.
    bucket = attachments_core.bucket()
    if bucket:
        for p in db.fetch_all(
            "SELECT storage_path FROM collab_piece WHERE storage_path IS NOT NULL AND (carte_id = %s "
            "OR commentaire_id IN (SELECT id FROM collab_commentaire WHERE carte_id = %s))",
            (carte_id, carte_id),
            role=user.role,
        ):
            storage.delete_object(bucket, str(p["storage_path"]))
    db.execute("DELETE FROM collab_carte WHERE id = %s", (carte_id,), role=user.role)
    db.execute(
        "UPDATE collab_tableau SET compteur_cartes = greatest(compteur_cartes - 1, 0) WHERE id = %s",
        (tableau_id,),
        role=user.role,
    )


class PublierActiviteIn(BaseModel):
    """Targeting for publishing a card as a real activity. ``general`` reaches the
    whole membership; a unit target reaches that unit through the same diffusion
    rules the back office uses. The date falls back to the card's own date."""

    cible_type: ShortStr = "general"
    cible_id: ShortStr | None = None
    debut: str | None = None
    lieu: LineStr | None = None
    type: ShortStr | None = None
    visibilite: ShortStr = "membres"


@router.post("/cartes-espace/{carte_id}/publier", response_model=CarteProtoOut)
def publier_carte_espace(
    carte_id: str,
    payload: PublierActiviteIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CarteProtoOut:
    """Publish a space card as a real activity through the shared activity engine.

    Any active space member (owner, admin or member) with the platform
    ``collaboration.gerer`` permission may publish; observers may not. The activity
    lands in the ``evenement`` table exactly like a back-office or pilotage activity,
    so it feeds the member agenda, attendance and questionnaires through the same
    targeting. Space members are then notified. Publishing is idempotent: a card
    already linked to an activity gets 409.
    """
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    carte = db.fetch_one(
        "SELECT titre, type_activite, debut, echeance, date_prevue, lieu, publie, evenement_id "
        "FROM collab_carte WHERE id = %s",
        (carte_id,),
        role=user.role,
    )
    if not carte:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="card not found")
    if carte["publie"] or carte["evenement_id"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="card already published")
    debut = payload.debut or carte["debut"] or carte["echeance"] or carte["date_prevue"]
    if not debut:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="date de debut requise pour publier une activite"
        )
    evenement_id = activites.inserer_evenement(
        titre=carte["titre"],
        type_=payload.type or carte["type_activite"],
        debut=debut,
        lieu=payload.lieu or carte["lieu"],
        cible_type=payload.cible_type,
        cible_id=payload.cible_id,
        visibilite=payload.visibilite,
        cree_par=user.id,
        role=user.role,
    )
    updated = db.execute(
        "UPDATE collab_carte SET publie = true, evenement_id = %s, "
        "type_activite = coalesce(%s, type_activite), maj_le = now() WHERE id = %s RETURNING id",
        (evenement_id, payload.type, carte_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not updated")
    audit.log(
        user.id, user.role, "publication_activite_collaboration", "evenement", evenement_id,
        {"carte_id": carte_id, "espace_id": espace_id, "cible_type": payload.cible_type},
    )
    _notifier_publication(carte_id, espace_id, str(carte["titre"]), user)
    return _fetch_carte(carte_id, user.role)


def _notifier_publication(carte_id: str, espace_id: str, titre: str, user: UserMe) -> None:
    """Tell every other space member the card was published as an activity."""
    ctx = _ctx_carte(carte_id, user.role)
    membres = db.fetch_all(
        "SELECT utilisateur_id FROM collab_espace_membre WHERE espace_id = %s", (espace_id,), role=user.role
    )
    for m in membres:
        uid = str(m["utilisateur_id"])
        if uid == user.id:
            continue
        collaboration_notif.emettre(
            uid, "publication", "collab_publication",
            f"L'activite « {titre} » a ete publiee et ajoutee a l'agenda", carte_id, espace_id, ctx, user.role,
        )
