"""Advanced checklist and checklist-item actions for collaboration cards.

Completes item-level parity on top of the basic create/toggle in
``collaboration_cartes_social``: rename and delete a checklist, reorder items,
rename an item, set or clear its assignee and due date, delete an item, and convert
an item into a full card in the same board. The parent card (and therefore the
space and role) is resolved server-side from the checklist/item, so the client never
has to pass it and cannot spoof it.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .collaboration_cartes import _espace_of_carte
from .collaboration_espaces import require_espace_role
from .fields import LineStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-checklists"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")


class ChecklistRenameIn(BaseModel):
    titre: LineStr


class ItemPatchIn(BaseModel):
    texte: str | None = None
    assigne_id: str | None = None
    echeance: str | None = None


class OrdreIn(BaseModel):
    ordre: list[str]


class ConvertirIn(BaseModel):
    colonne_id: str | None = None


def _carte_of_checklist(checklist_id: str, role: str) -> str:
    row = db.fetch_one("SELECT carte_id FROM collab_checklist WHERE id = %s", (checklist_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="checklist not found")
    return str(row["carte_id"])


def _carte_and_checklist_of_item(item_id: str, role: str) -> tuple[str, str]:
    row = db.fetch_one(
        "SELECT ci.checklist_id, cl.carte_id FROM collab_checklist_item ci "
        "JOIN collab_checklist cl ON cl.id = ci.checklist_id WHERE ci.id = %s",
        (item_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")
    return str(row["carte_id"]), str(row["checklist_id"])


def _auth(carte_id: str, user: UserMe) -> str:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    return espace_id


@router.patch("/checklists/{checklist_id}", status_code=status.HTTP_204_NO_CONTENT)
def renommer_checklist(
    checklist_id: str,
    payload: ChecklistRenameIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _auth(_carte_of_checklist(checklist_id, user.role), user)
    db.execute("UPDATE collab_checklist SET titre = %s WHERE id = %s", (payload.titre, checklist_id), role=user.role)


@router.delete("/checklists/{checklist_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_checklist(
    checklist_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _auth(_carte_of_checklist(checklist_id, user.role), user)
    db.execute("DELETE FROM collab_checklist_item WHERE checklist_id = %s", (checklist_id,), role=user.role)
    db.execute("DELETE FROM collab_checklist WHERE id = %s", (checklist_id,), role=user.role)


@router.post("/checklists/{checklist_id}/items/ordre", status_code=status.HTTP_204_NO_CONTENT)
def reordonner_items(
    checklist_id: str,
    payload: OrdreIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    _auth(_carte_of_checklist(checklist_id, user.role), user)
    for index, item_id in enumerate(payload.ordre):
        db.execute(
            "UPDATE collab_checklist_item SET position = %s WHERE id = %s AND checklist_id = %s",
            (index, item_id, checklist_id),
            role=user.role,
        )


@router.patch("/checklist-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def modifier_item(
    item_id: str,
    payload: ItemPatchIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    carte_id, _ = _carte_and_checklist_of_item(item_id, user.role)
    espace_id = _auth(carte_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if "texte" in fields and fields["texte"]:
        db.execute(
            "UPDATE collab_checklist_item SET texte = %s WHERE id = %s", (fields["texte"], item_id), role=user.role
        )
    if "assigne_id" in fields:
        assignee = fields["assigne_id"] or None
        if assignee:
            member = db.fetch_one(
                "SELECT 1 FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
                (espace_id, assignee),
                role=user.role,
            )
            if not member:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="assignee not a space member")
        db.execute(
            "UPDATE collab_checklist_item SET assigne_id = %s::uuid WHERE id = %s", (assignee, item_id), role=user.role
        )
    if "echeance" in fields:
        db.execute(
            "UPDATE collab_checklist_item SET echeance = %s::timestamptz WHERE id = %s",
            (fields["echeance"] or None, item_id),
            role=user.role,
        )


@router.delete("/checklist-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_item(
    item_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    carte_id, _ = _carte_and_checklist_of_item(item_id, user.role)
    _auth(carte_id, user)
    db.execute("DELETE FROM collab_checklist_item WHERE id = %s", (item_id,), role=user.role)


@router.post("/checklist-items/{item_id}/convertir", status_code=status.HTTP_201_CREATED)
def convertir_item_en_carte(
    item_id: str,
    payload: ConvertirIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, str]:
    carte_id, _ = _carte_and_checklist_of_item(item_id, user.role)
    _auth(carte_id, user)
    item = db.fetch_one("SELECT texte FROM collab_checklist_item WHERE id = %s", (item_id,), role=user.role)
    parent = db.fetch_one(
        "SELECT tableau_id, colonne_id FROM collab_carte WHERE id = %s", (carte_id,), role=user.role
    )
    if not item or not parent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item or card not found")
    tableau_id = str(parent["tableau_id"])
    colonne_id = payload.colonne_id or str(parent["colonne_id"])
    created = db.execute(
        "INSERT INTO collab_carte (tableau_id, colonne_id, titre, position, numero, cree_par) "
        "VALUES (%s, %s, %s, "
        "(SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
        "(SELECT coalesce(max(numero), 0) + 1 FROM collab_carte WHERE tableau_id = %s), %s) RETURNING id",
        (tableau_id, colonne_id, item["texte"][:200], colonne_id, tableau_id, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="card not created")
    new_id = str(created["id"])
    db.execute(
        "INSERT INTO collab_carte_membre (carte_id, utilisateur_id, role) VALUES (%s, %s, 'suiveur') "
        "ON CONFLICT DO NOTHING",
        (new_id, user.id),
        role=user.role,
    )
    db.execute(
        "INSERT INTO collab_activite (carte_id, auteur_id, texte) VALUES (%s, %s, %s)",
        (new_id, user.id, "a converti un element de checklist en carte"),
        role=user.role,
    )
    db.execute(
        "UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s",
        (tableau_id,),
        role=user.role,
    )
    db.execute("DELETE FROM collab_checklist_item WHERE id = %s", (item_id,), role=user.role)
    audit.log(user.id, user.role, "conversion_item_carte", "collab_carte", new_id, {"depuis": carte_id})
    return {"id": new_id}
