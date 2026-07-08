"""Card comments, reactions and checklists for the collaboration spaces.

Splits the social mutations off the card module to keep each file small. A comment
records @-mentions and notifies mentioned members and followers; reactions and
read receipts are per account; checklists and their items track preparation work.
Collaborator identity is the login account (utilisateur). Space role enforced via
the shared helpers.
"""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import collaboration_notif, db
from .collaboration_cartes import CommentaireProtoOut, _espace_of_carte
from .collaboration_espaces import require_espace_role
from .fields import LineStr, TextStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-cartes-social"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")
COMMENTATEURS = ("proprietaire", "admin", "membre", "observateur")
_MENTION_RE = re.compile(r"@([\w.\-]{2,40})")


class CommentaireIn(BaseModel):
    corps: TextStr


class ReactionIn(BaseModel):
    carte_id: LineStr
    type: LineStr


class ChecklistIn(BaseModel):
    titre: LineStr


class ChecklistItemIn(BaseModel):
    carte_id: LineStr
    texte: LineStr


class ChecklistItemToggleIn(BaseModel):
    carte_id: LineStr
    checklist_id: LineStr


def _espace_id_or_403(carte_id: str, user: UserMe, allowed: tuple[str, ...]) -> str:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, allowed)
    return espace_id


def _resolve_mentions(corps: str, espace_id: str, role: str) -> list[str]:
    """Match @tokens in the comment against the space members' display names or
    e-mail local part, returning the matched utilisateur ids. Best effort: an
    unmatched token is ignored, never raised."""
    tokens = {t.lower() for t in _MENTION_RE.findall(corps)}
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
        if any(tok in (local, nom) or local.startswith(tok) or (nom and nom.startswith(tok)) for tok in tokens):
            matched.append(str(r["id"]))
    return matched


@router.post(
    "/cartes-espace/{carte_id}/commentaires",
    response_model=CommentaireProtoOut,
    status_code=status.HTTP_201_CREATED,
)
def ajouter_commentaire(
    carte_id: str,
    payload: CommentaireIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> CommentaireProtoOut:
    espace_id = _espace_id_or_403(carte_id, user, COMMENTATEURS)
    created = db.execute(
        "INSERT INTO collab_commentaire (carte_id, auteur_id, auteur_nom, corps) VALUES (%s, %s, %s, %s) "
        "RETURNING id, auteur_id, corps, cree_le, edite_le",
        (carte_id, user.id, user.email, payload.corps),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="comment not created")
    cid = str(created["id"])
    # Author has read their own comment.
    db.execute(
        "INSERT INTO collab_commentaire_lu (commentaire_id, utilisateur_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (cid, user.id),
        role=user.role,
    )
    titre_row = db.fetch_one("SELECT titre FROM collab_carte WHERE id = %s", (carte_id,), role=user.role)
    titre = titre_row["titre"] if titre_row else ""
    espace_nom = collaboration_notif.nom_espace(espace_id, user.role)
    ctx = {"titre": titre, "espace": espace_nom}
    db.execute(
        "INSERT INTO collab_activite (carte_id, auteur_id, texte) VALUES (%s, %s, 'a commente la carte')",
        (carte_id, user.id),
        role=user.role,
    )
    mentions = _resolve_mentions(payload.corps, espace_id, user.role)
    for mid in mentions:
        db.execute(
            "INSERT INTO collab_commentaire_mention (commentaire_id, utilisateur_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (cid, mid),
            role=user.role,
        )
        if mid != user.id:
            # Mention: in-app + real channels (email / Telegram / WhatsApp).
            collaboration_notif.emettre(
                mid, "mention", "collab_mention", f"Vous etes mentionne dans « {titre} »",
                carte_id, espace_id, ctx, user.role,
            )
    suiveurs = db.fetch_all(
        "SELECT utilisateur_id FROM collab_carte_membre WHERE carte_id = %s AND role = 'suiveur'",
        (carte_id,),
        role=user.role,
    )
    for s in suiveurs:
        sid = str(s["utilisateur_id"])
        if sid != user.id and sid not in mentions:
            # Follow notification: in-app only (high volume, off-channel would be noise).
            collaboration_notif.emettre(
                sid, "carte_suivie", None, f"Nouveau commentaire sur « {titre} »", carte_id, espace_id, None, user.role
            )
    return CommentaireProtoOut(
        id=cid,
        auteur_id=str(created["auteur_id"]) if created["auteur_id"] else "",
        corps=created["corps"],
        cree_le=created["cree_le"].isoformat() if created["cree_le"] else None,
        edite_le=created["edite_le"].isoformat() if created["edite_le"] else None,
        reactions=[],
        pieces=[],
        lu_par=[user.id],
        mentions=mentions,
    )


@router.post("/cartes-espace/{carte_id}/lu", status_code=status.HTTP_204_NO_CONTENT)
def marquer_lus(
    carte_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))]
) -> None:
    _espace_id_or_403(carte_id, user, COMMENTATEURS)
    db.execute(
        "INSERT INTO collab_commentaire_lu (commentaire_id, utilisateur_id) "
        "SELECT id, %s FROM collab_commentaire WHERE carte_id = %s ON CONFLICT DO NOTHING",
        (user.id, carte_id),
        role=user.role,
    )
    db.execute(
        "UPDATE collab_notification SET lue = true WHERE carte_id = %s AND utilisateur_id = %s AND NOT lue",
        (carte_id, user.id),
        role=user.role,
    )


@router.post("/commentaires/{commentaire_id}/reaction", status_code=status.HTTP_204_NO_CONTENT)
def reagir(
    commentaire_id: str,
    payload: ReactionIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _espace_id_or_403(payload.carte_id, user, COMMENTATEURS)
    existing = db.fetch_one(
        "SELECT 1 FROM collab_reaction WHERE commentaire_id = %s AND utilisateur_id = %s AND type = %s",
        (commentaire_id, user.id, payload.type),
        role=user.role,
    )
    if existing:
        db.execute(
            "DELETE FROM collab_reaction WHERE commentaire_id = %s AND utilisateur_id = %s AND type = %s",
            (commentaire_id, user.id, payload.type),
            role=user.role,
        )
    else:
        db.execute(
            "INSERT INTO collab_reaction (commentaire_id, utilisateur_id, type) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (commentaire_id, user.id, payload.type),
            role=user.role,
        )


@router.post("/cartes-espace/{carte_id}/checklists", status_code=status.HTTP_204_NO_CONTENT)
def ajouter_checklist(
    carte_id: str,
    payload: ChecklistIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _espace_id_or_403(carte_id, user, MEMBRES_ACTIFS)
    db.execute(
        "INSERT INTO collab_checklist (carte_id, titre, position) "
        "VALUES (%s, %s, (SELECT coalesce(max(position), -1) + 1 FROM collab_checklist WHERE carte_id = %s))",
        (carte_id, payload.titre, carte_id),
        role=user.role,
    )


@router.post("/checklists/{checklist_id}/items", status_code=status.HTTP_204_NO_CONTENT)
def ajouter_checklist_item(
    checklist_id: str,
    payload: ChecklistItemIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _espace_id_or_403(payload.carte_id, user, MEMBRES_ACTIFS)
    db.execute(
        "INSERT INTO collab_checklist_item (checklist_id, texte, position) "
        "VALUES (%s, %s, (SELECT coalesce(max(position), -1) + 1 FROM collab_checklist_item WHERE checklist_id = %s))",
        (checklist_id, payload.texte, checklist_id),
        role=user.role,
    )


@router.post("/checklist-items/{item_id}/basculer", status_code=status.HTTP_204_NO_CONTENT)
def basculer_item(
    item_id: str,
    payload: ChecklistItemToggleIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _espace_id_or_403(payload.carte_id, user, MEMBRES_ACTIFS)
    db.execute(
        "UPDATE collab_checklist_item SET fait = NOT fait WHERE id = %s AND checklist_id = %s",
        (item_id, payload.checklist_id),
        role=user.role,
    )
