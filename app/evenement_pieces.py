"""Shared logic for activity/event attachments (images and files).

An attachment is stored inline as a data URL on ``evenement_piece`` (same approach
as the collaboration card attachments), size-capped here. The back office and the
collaboration app expose their own routes (each with its own permission) that all
call these helpers, so the behaviour is identical on both sides with no duplicated
logic.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel

from . import db

# Inline data URLs bloat the row, so cap the raw file size. ~4/3 base64 overhead is
# accounted for: 3.5 MB of data URL is roughly a 2.5 MB file.
_MAX_DATA_URL = 3_500_000


class PieceEvenementOut(BaseModel):
    id: str
    nom: str
    type: str
    taille: int
    url: str
    cree_le: str | None = None


class PieceEvenementIn(BaseModel):
    nom: str
    type: str = ""
    taille: int = 0
    data_url: str


def _row(r: dict[str, Any]) -> PieceEvenementOut:
    cree = r.get("cree_le")
    return PieceEvenementOut(
        id=str(r["id"]),
        nom=r["nom"],
        type=r.get("type") or "",
        taille=int(r.get("taille") or 0),
        url=r["url"],
        cree_le=cree.isoformat() if hasattr(cree, "isoformat") else None,
    )


def lister_pieces(evenement_id: str, role: str | None) -> list[PieceEvenementOut]:
    rows = db.fetch_all(
        "SELECT id, nom, type, taille, url, cree_le FROM evenement_piece "
        "WHERE evenement_id = %s ORDER BY cree_le",
        (evenement_id,),
        role=role,
    )
    return [_row(r) for r in rows]


def ajouter_piece(evenement_id: str, payload: PieceEvenementIn, cree_par: str, role: str | None) -> PieceEvenementOut:
    if not db.fetch_one("SELECT id FROM evenement WHERE id = %s", (evenement_id,), role=role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activity not found")
    if not payload.data_url or not payload.data_url.startswith("data:"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pièce invalide (data URL attendue)")
    if len(payload.data_url) > _MAX_DATA_URL:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="fichier trop volumineux (max ~2,5 Mo)"
        )
    nom = (payload.nom or "piece").strip()[:200]
    created = db.execute(
        "INSERT INTO evenement_piece (evenement_id, nom, type, taille, url, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, nom, type, taille, url, cree_le",
        (evenement_id, nom, (payload.type or "")[:120], int(payload.taille or 0), payload.data_url, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="pièce non enregistrée")
    return _row(created)


def supprimer_piece(piece_id: str, role: str | None) -> None:
    db.execute("DELETE FROM evenement_piece WHERE id = %s", (piece_id,), role=role)
