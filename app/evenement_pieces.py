"""Shared logic for activity/event attachments (images and files).

Two storage modes, transparent to the client (which always sends a data URL):

- Inline (default): the attachment is stored as a data URL on ``evenement_piece``,
  size-capped here. No bucket required.
- Object storage (when ``ADSUM_STORAGE_BUCKET_EVENEMENTS`` is set): the API decodes
  the data URL and uploads the bytes to a private bucket, storing only the object
  path; reads mint a short-lived signed URL. This keeps base64 blobs out of the
  database. Existing inline rows keep working (``storage_path`` is NULL for them).

The back office and the collaboration app expose their own routes (each with its own
permission) that all call these helpers, so behaviour is identical on both sides.
"""
from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel

from . import db, storage
from .config import settings

# Inline data URLs bloat the row, so cap the raw file size. ~4/3 base64 overhead is
# accounted for: 3.5 MB of data URL is roughly a 2.5 MB file. The cap applies to the
# request body in both modes (the client always sends a data URL).
_MAX_DATA_URL = 3_500_000
# Signed download URLs are minted per read; an hour comfortably covers a page view.
_SIGNED_URL_TTL = 3600


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


def _bucket() -> str:
    """Object-storage bucket for attachments, or "" when inline mode is in force."""
    if settings.storage_bucket_evenements and settings.supabase_url and settings.supabase_service_key:
        return settings.storage_bucket_evenements
    return ""


def _url_for(r: dict[str, Any]) -> str:
    """Resolve the URL a client can fetch: a signed URL for object-backed rows, or
    the stored inline data URL otherwise. A storage failure degrades to an empty
    URL rather than raising, so listing the other attachments still succeeds."""
    path = r.get("storage_path")
    if path:
        try:
            return storage.signed_download_url(settings.storage_bucket_evenements, str(path), _SIGNED_URL_TTL)
        except (storage.StorageError, OSError):  # URLError (DNS/connection) subclasses OSError
            return ""
    return r.get("url") or ""


def _row(r: dict[str, Any]) -> PieceEvenementOut:
    cree = r.get("cree_le")
    return PieceEvenementOut(
        id=str(r["id"]),
        nom=r["nom"],
        type=r.get("type") or "",
        taille=int(r.get("taille") or 0),
        url=_url_for(r),
        cree_le=cree.isoformat() if hasattr(cree, "isoformat") else None,
    )


def lister_pieces(evenement_id: str, role: str | None) -> list[PieceEvenementOut]:
    # ``storage_path`` only exists once migration 0104 is applied, which the owner
    # does together with setting the bucket. In inline mode the column is never
    # referenced, so the read works with or without the migration (no ordering
    # hazard when deploying the API before opting into object storage).
    cols = "id, nom, type, taille, url, storage_path, cree_le" if _bucket() else "id, nom, type, taille, url, cree_le"
    rows = db.fetch_all(
        f"SELECT {cols} FROM evenement_piece WHERE evenement_id = %s ORDER BY cree_le",
        (evenement_id,),
        role=role,
    )
    return [_row(r) for r in rows]


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Return the raw bytes and MIME type of a ``data:<mime>;base64,<payload>`` URL."""
    header, _, payload = data_url.partition(",")
    mime = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError subclass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="pièce invalide (encodage base64)"
        ) from exc
    return raw, mime


def _inserer_inline(
    evenement_id: str, nom: str, type_: str, taille: int, data_url: str, cree_par: str, role: str | None
) -> dict[str, Any]:
    """Inline insert (data URL only). Never touches ``storage_path`` so it works
    with or without migration 0104."""
    created = db.execute(
        "INSERT INTO evenement_piece (evenement_id, nom, type, taille, url, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, nom, type, taille, url, cree_le",
        (evenement_id, nom, type_, taille, data_url, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="pièce non enregistrée")
    return created


def _inserer_storage(
    evenement_id: str, piece_id: str, nom: str, type_: str, taille: int, path: str, cree_par: str, role: str | None
) -> dict[str, Any]:
    """Object-storage insert: keeps only the path, with the row id matching the
    object path. Requires migration 0104 (applied together with the bucket)."""
    created = db.execute(
        "INSERT INTO evenement_piece (id, evenement_id, nom, type, taille, url, storage_path, cree_par) "
        "VALUES (%s::uuid, %s, %s, %s, %s, '', %s, %s) "
        "RETURNING id, nom, type, taille, url, storage_path, cree_le",
        (piece_id, evenement_id, nom, type_, taille, path, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="pièce non enregistrée")
    return created


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
    type_ = (payload.type or "")[:120]
    taille = int(payload.taille or 0)
    bucket = _bucket()
    if bucket:
        # Object-storage mode: upload the decoded bytes, persist only the path. On a
        # storage failure fall back to the inline data URL so an upload never breaks.
        raw, mime = _decode_data_url(payload.data_url)
        piece_id = str(uuid.uuid4())
        path = f"{evenement_id}/{piece_id}"
        try:
            storage.upload_bytes(bucket, path, raw, type_ or mime or "application/octet-stream")
            return _row(_inserer_storage(evenement_id, piece_id, nom, type_, taille, path, cree_par, role))
        except storage.StorageError:
            pass
    return _row(_inserer_inline(evenement_id, nom, type_, taille, payload.data_url, cree_par, role))


def supprimer_piece(piece_id: str, role: str | None) -> None:
    if _bucket():
        row = db.fetch_one("SELECT storage_path FROM evenement_piece WHERE id = %s", (piece_id,), role=role)
        if row and row.get("storage_path"):
            storage.delete_object(settings.storage_bucket_evenements, str(row["storage_path"]))
    db.execute("DELETE FROM evenement_piece WHERE id = %s", (piece_id,), role=role)
