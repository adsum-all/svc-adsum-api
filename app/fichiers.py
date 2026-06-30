"""Member file endpoints (profile photo, identity documents, consents).

The client asks the API for a short-lived signed upload URL, PUTs the file
straight to the private Supabase bucket, then confirms; the API records the
storage path in PostgreSQL. Downloads go through short-lived signed URLs. The
service key never reaches the browser. Backed by the 0012 schema.
"""
# ruff: noqa: E501
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db, storage
from .auth import current_user
from .config import settings
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["fichiers"])

PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}
DOC_EXTS = {"jpg", "jpeg", "png", "webp", "pdf"}
DOC_TYPES = {"piece_identite", "passeport", "permis", "carte_consulaire", "justificatif_domicile", "photo_identite", "autre"}


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


class PhotoConfirm(BaseModel):
    path: str


class UploadUrlIn(BaseModel):
    type: str
    ext: str = "jpg"


class DocConfirm(BaseModel):
    type: str
    path: str
    nom_fichier: str | None = None
    mime: str | None = None


def _safe_ext(ext: str, allowed: set[str]) -> str:
    cleaned = ext.lower().lstrip(".")
    if cleaned not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported file extension")
    return "jpg" if cleaned == "jpeg" else cleaned


# --- Profile photo ---------------------------------------------------------

@router.post("/photo/upload-url")
def photo_upload_url(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str]:
    membre_id, _ = ctx
    path = f"{membre_id}/photo.jpg"
    try:
        return storage.signed_upload_url(settings.storage_bucket_photos, path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable") from exc


@router.post("/photo/confirm", status_code=status.HTTP_204_NO_CONTENT)
def photo_confirm(payload: PhotoConfirm, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> None:
    membre_id, role = ctx
    if not payload.path.startswith(f"{membre_id}/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    db.execute("UPDATE membre SET photo_url = %s WHERE id = %s", (payload.path, membre_id), role=role)


@router.get("/photo")
def photo_get(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str | None]:
    membre_id, role = ctx
    row = db.fetch_one("SELECT photo_url FROM membre WHERE id = %s", (membre_id,), role=role)
    path = row.get("photo_url") if row else None
    if not path:
        return {"url": None}
    try:
        return {"url": storage.signed_download_url(settings.storage_bucket_photos, str(path))}
    except storage.StorageError:
        return {"url": None}


# --- Identity documents / consents ----------------------------------------

@router.post("/documents/upload-url")
def doc_upload_url(payload: UploadUrlIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str]:
    membre_id, _ = ctx
    if payload.type not in DOC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown document type")
    ext = _safe_ext(payload.ext, DOC_EXTS)
    path = f"{membre_id}/{payload.type}-{uuid.uuid4().hex[:8]}.{ext}"
    try:
        return storage.signed_upload_url(settings.storage_bucket_documents, path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable") from exc


@router.post("/documents/confirm", status_code=status.HTTP_201_CREATED)
def doc_confirm(payload: DocConfirm, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str]:
    membre_id, role = ctx
    if payload.type not in DOC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown document type")
    if not payload.path.startswith(f"{membre_id}/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    created = db.execute(
        "INSERT INTO document (membre_id, type, statut, bucket, chemin_stockage, nom_fichier, mime, recu_le) "
        "VALUES (%s, %s, 'recu', %s, %s, %s, %s, now()) RETURNING id",
        (membre_id, payload.type, settings.storage_bucket_documents, payload.path, payload.nom_fichier, payload.mime),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="document not registered")
    return {"id": str(created["id"])}


@router.get("/documents/{document_id}/url")
def doc_download_url(document_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str | None]:
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT bucket, chemin_stockage FROM document WHERE id = %s AND membre_id = %s",
        (document_id, membre_id),
        role=role,
    )
    if not row or not row.get("chemin_stockage"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    try:
        url = storage.signed_download_url(str(row["bucket"] or settings.storage_bucket_documents), str(row["chemin_stockage"]))
        return {"url": url}
    except storage.StorageError:
        return {"url": None}
