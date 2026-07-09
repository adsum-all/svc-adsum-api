"""Attachments (images and files) for collaboration cards and comments.

Adds the write side that was missing: upload an attachment to a card or a comment,
delete it, and set/clear a card cover image. Reads stay in ``collaboration_cartes``
(embedded in the card and comment payloads). Storage and security are shared with
event attachments through ``attachments_core`` (active-content refusal, inline vs
download policy, private-bucket object storage with signed URLs, inline fallback).
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import attachments_core as att
from . import audit, db, storage
from .collaboration_cartes import PieceOut, _espace_of_carte
from .collaboration_espaces import require_espace_role
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-pieces"])

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")


class PieceIn(BaseModel):
    nom: str
    type: str = ""
    taille: int = 0
    data_url: str


class CouvertureIn(BaseModel):
    piece_id: str | None = None


def _piece_out(row: dict[str, Any]) -> PieceOut:
    return PieceOut(
        id=str(row["id"]),
        nom=row["nom"],
        taille=int(row.get("taille") or 0),
        type=row.get("type") or "",
        cree_le=row["cree_le"].isoformat() if row.get("cree_le") else None,
        data_url=att.served_url(row.get("url"), row.get("storage_path"), row["nom"], row.get("type") or ""),
        couverture=False,
    )


def _insert(ref_col: str, ref_id: str, nom: str, type_: str, data_url: str, cree_par: str, role: str) -> dict[str, Any]:
    """Insert an attachment, uploading to object storage when a bucket is configured
    (real byte size + validated content type stored), else inline as a data URL. A
    storage failure falls back to inline so an upload never breaks. ``ref_col`` is a
    controlled literal ("carte_id" or "commentaire_id"), never user input."""
    bucket = att.bucket()
    if bucket:
        raw = att.decode_data_url(data_url)
        content_type = att.stored_content_type(att.data_url_mime(data_url), type_)
        piece_id = str(uuid.uuid4())
        path = f"cartes/{piece_id}"
        try:
            storage.upload_bytes(bucket, path, raw, content_type)
        except (storage.StorageError, OSError):
            pass
        else:
            try:
                created = db.execute(
                    f"INSERT INTO collab_piece (id, {ref_col}, nom, taille, type, url, storage_path, cree_par) "
                    "VALUES (%s::uuid, %s, %s, %s, %s, '', %s, %s) "
                    "RETURNING id, nom, taille, type, url, storage_path, cree_le",
                    (piece_id, ref_id, nom, len(raw), content_type, path, cree_par),
                    role=role,
                )
                if not created:
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="piece not created")
                return created
            except Exception:
                storage.delete_object(bucket, path)  # no orphan object on a failed insert
                raise
    created = db.execute(
        f"INSERT INTO collab_piece ({ref_col}, nom, taille, type, url, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, nom, taille, type, url, cree_le",
        (ref_id, nom, int(_len_hint(data_url)), (type_ or "")[:120], data_url, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="piece not created")
    return created


def _len_hint(data_url: str) -> int:
    """Rough byte size of an inline data URL payload (base64 -> ~3/4)."""
    payload = data_url.split(",", 1)[1] if "," in data_url else ""
    return max(0, (len(payload) * 3) // 4)


def _log(carte_id: str, user: UserMe, action: str, nom: str) -> None:
    audit.log(user.id, user.role, action, "collab_carte", carte_id, {"nom": nom})
    db.execute(
        "INSERT INTO collab_activite (carte_id, auteur_id, texte) VALUES (%s, %s, 'a ajoute une piece jointe')",
        (carte_id, user.id),
        role=user.role,
    )


@router.post(
    "/cartes-espace/{carte_id}/pieces", response_model=PieceOut, status_code=status.HTTP_201_CREATED
)
def ajouter_piece_carte(
    carte_id: uuid.UUID,
    payload: PieceIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> PieceOut:
    cid = str(carte_id)
    _, espace_id = _espace_of_carte(cid, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    att.validate_payload(payload.data_url, payload.type)
    nom = (payload.nom or "piece").strip()[:200]
    created = _insert("carte_id", cid, nom, payload.type, payload.data_url, user.id, user.role)
    _log(cid, user, "ajout_piece_carte", nom)
    return _piece_out(created)


@router.post("/commentaires/{commentaire_id}/pieces", response_model=PieceOut, status_code=status.HTTP_201_CREATED)
def ajouter_piece_commentaire(
    commentaire_id: uuid.UUID,
    payload: PieceIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> PieceOut:
    cmt = db.fetch_one("SELECT carte_id FROM collab_commentaire WHERE id = %s", (str(commentaire_id),), role=user.role)
    if not cmt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="comment not found")
    carte_id = str(cmt["carte_id"])
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    att.validate_payload(payload.data_url, payload.type)
    nom = (payload.nom or "piece").strip()[:200]
    created = _insert("commentaire_id", str(commentaire_id), nom, payload.type, payload.data_url, user.id, user.role)
    _log(carte_id, user, "ajout_piece_commentaire", nom)
    return _piece_out(created)


@router.delete("/pieces/{piece_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_piece(
    piece_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    row = db.fetch_one(
        "SELECT p.nom, p.storage_path, p.carte_id, c.carte_id AS commentaire_carte "
        "FROM collab_piece p LEFT JOIN collab_commentaire c ON c.id = p.commentaire_id WHERE p.id = %s",
        (str(piece_id),),
        role=user.role,
    )
    if not row:
        return
    carte_id = str(row["carte_id"] or row["commentaire_carte"] or "")
    if carte_id:
        _, espace_id = _espace_of_carte(carte_id, user.role)
        require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    if row.get("storage_path") and att.bucket():
        storage.delete_object(att.bucket(), str(row["storage_path"]))
    # A cover pointing at this piece is cleared so the card keeps no dangling ref.
    if carte_id:
        db.execute(
            "UPDATE collab_carte SET couverture_id = NULL WHERE id = %s AND couverture_id = %s",
            (carte_id, str(piece_id)),
            role=user.role,
        )
    db.execute("DELETE FROM collab_piece WHERE id = %s", (str(piece_id),), role=user.role)
    if carte_id:
        audit.log(user.id, user.role, "suppression_piece_carte", "collab_carte", carte_id, {"nom": row.get("nom")})


@router.post("/cartes-espace/{carte_id}/couverture", status_code=status.HTTP_204_NO_CONTENT)
def definir_couverture(
    carte_id: uuid.UUID,
    payload: CouvertureIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> None:
    cid = str(carte_id)
    _, espace_id = _espace_of_carte(cid, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    if payload.piece_id:
        owns = db.fetch_one(
            "SELECT 1 FROM collab_piece WHERE id = %s AND carte_id = %s", (payload.piece_id, cid), role=user.role
        )
        if not owns:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="attachment not on this card")
    db.execute(
        "UPDATE collab_carte SET couverture_id = %s WHERE id = %s",
        (payload.piece_id, cid),
        role=user.role,
    )
