"""Advanced checklist and checklist-item actions for collaboration cards.

Completes item-level parity on top of the basic create/toggle in
``collaboration_cartes_social``: rename and delete a checklist, reorder items,
rename an item, set or clear its (multiple) assignees and due date, delete an item,
and convert an item into a full card in the same board. The parent card (and
therefore the space and role) is resolved server-side, so the client cannot spoof
it. Multi-statement operations run in a single transaction so a partial failure
never leaves an item half-assigned or a converted card orphaned.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .collaboration_cartes import _espace_of_carte, _valider_colonne
from .collaboration_espaces import require_espace_role
from .fields import LineStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-checklists"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")


class ChecklistRenameIn(BaseModel):
    titre: LineStr


class ItemPatchIn(BaseModel):
    # Typed so malformed input is rejected as 422 at the boundary, never a DB 500.
    texte: str | None = None
    assigne_id: UUID | None = None
    assignes: list[UUID] | None = None
    echeance: datetime | None = None


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


def _require_membre(espace_id: str, uid: str, role: str) -> None:
    member = db.fetch_one(
        "SELECT 1 FROM collab_espace_membre WHERE espace_id = %s AND utilisateur_id = %s",
        (espace_id, uid),
        role=role,
    )
    if not member:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="assignee not a space member")


def _sync_item_assignes(item_id: str, espace_id: str, assignes: list[str] | None, role: str) -> None:
    """Replace an item's assignee set (validated members) and keep the legacy single
    assigne_id in sync as the first assignee. Runs in one transaction so a reader can
    never observe the item mid-swap and a partial failure rolls back entirely."""
    ids = [a for a in dict.fromkeys(assignes or []) if a]
    for a in ids:
        _require_membre(espace_id, a, role)
    with db.connection(role=role) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM collab_item_assigne WHERE item_id = %s", (item_id,))
        for a in ids:
            cur.execute(
                "INSERT INTO collab_item_assigne (item_id, utilisateur_id) VALUES (%s, %s::uuid) "
                "ON CONFLICT DO NOTHING",
                (item_id, a),
            )
        cur.execute(
            "UPDATE collab_checklist_item SET assigne_id = %s::uuid WHERE id = %s", (ids[0] if ids else None, item_id)
        )


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
    # One atomic reindex of EVERY item: listed items take the client order, omitted
    # ones keep their prior order after them, so positions stay unique 0..N-1 even if
    # the ordre list is partial or contains duplicates.
    db.execute(
        "UPDATE collab_checklist_item ci SET position = sub.new_pos FROM ("
        "  SELECT id, row_number() OVER ("
        "    ORDER BY array_position(%s::uuid[], id) NULLS LAST, position, id) - 1 AS new_pos"
        "  FROM collab_checklist_item WHERE checklist_id = %s"
        ") sub WHERE ci.id = sub.id AND ci.checklist_id = %s",
        (payload.ordre, checklist_id, checklist_id),
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
    if "assignes" in fields:
        _sync_item_assignes(item_id, espace_id, [str(a) for a in (fields["assignes"] or [])], user.role)
    elif "assigne_id" in fields:
        # Legacy single-field patch must not wipe co-assignees: promote it to primary
        # (first) while preserving the rest; a null primary is a no-op on the set.
        primary = str(fields["assigne_id"]) if fields["assigne_id"] else None
        existing = [
            str(r["utilisateur_id"])
            for r in db.fetch_all(
                "SELECT utilisateur_id FROM collab_item_assigne WHERE item_id = %s", (item_id,), role=user.role
            )
        ]
        merged = ([primary] + [x for x in existing if x != primary]) if primary else existing
        _sync_item_assignes(item_id, espace_id, merged, user.role)
    if "echeance" in fields:
        db.execute(
            "UPDATE collab_checklist_item SET echeance = %s WHERE id = %s",
            (fields["echeance"], item_id),
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
    item = db.fetch_one(
        "SELECT ci.texte, ci.echeance, "
        "coalesce(array_agg(ia.utilisateur_id::text) FILTER (WHERE ia.utilisateur_id IS NOT NULL), '{}') AS assignes "
        "FROM collab_checklist_item ci LEFT JOIN collab_item_assigne ia ON ia.item_id = ci.id "
        "WHERE ci.id = %s GROUP BY ci.texte, ci.echeance",
        (item_id,),
        role=user.role,
    )
    parent = db.fetch_one(
        "SELECT tableau_id, colonne_id FROM collab_carte WHERE id = %s", (carte_id,), role=user.role
    )
    if not item or not parent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item or card not found")
    tableau_id = str(parent["tableau_id"])
    if payload.colonne_id:
        _valider_colonne(payload.colonne_id, tableau_id, user.role)
        colonne_id = payload.colonne_id
    else:
        colonne_id = str(parent["colonne_id"])
    assignes = [str(a) for a in (item["assignes"] or [])]
    # One transaction: create the card (carrying the due date), attach members
    # (self + the item's assignees), log activity, bump the counter, drop the item.
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collab_carte (tableau_id, colonne_id, titre, echeance, position, numero, cree_par) "
            "VALUES (%s, %s, %s, %s, "
            "(SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
            "(SELECT coalesce(max(numero), 0) + 1 FROM collab_carte WHERE tableau_id = %s), %s) RETURNING id",
            (tableau_id, colonne_id, item["texte"][:200], item["echeance"], colonne_id, tableau_id, user.id),
        )
        row = cur.fetchone()
        new_id = str(row["id"])
        for uid in dict.fromkeys([user.id, *assignes]):
            role_membre = "assigne" if uid in assignes else "suiveur"
            cur.execute(
                "INSERT INTO collab_carte_membre (carte_id, utilisateur_id, role) VALUES (%s, %s::uuid, %s) "
                "ON CONFLICT DO NOTHING",
                (new_id, uid, role_membre),
            )
        cur.execute(
            "INSERT INTO collab_activite (carte_id, auteur_id, texte) VALUES (%s, %s, %s)",
            (new_id, user.id, "a converti un element de checklist en carte"),
        )
        cur.execute("UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s", (tableau_id,))
        cur.execute("DELETE FROM collab_checklist_item WHERE id = %s", (item_id,))
    audit.log(user.id, user.role, "conversion_item_carte", "collab_carte", new_id, {"depuis": carte_id})
    return {"id": new_id}
