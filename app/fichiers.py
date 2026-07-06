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

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from . import audit, db, storage
from .auth import current_user
from .config import settings
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["fichiers"])

PHOTO_EXTS = {"jpg", "jpeg", "png", "webp"}
DOC_EXTS = {"jpg", "jpeg", "png", "webp", "pdf"}
DOC_TYPES = {"piece_identite", "passeport", "permis", "carte_consulaire", "justificatif_domicile", "photo_identite", "attestation", "autre"}


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


class PhotoConfirm(BaseModel):
    path: str
    phash: str | None = None  # 16-hex-char dHash computed on the client
    focus_x: int | None = None  # face focal point, 0-100 (CSS object-position X)
    focus_y: int | None = None  # face focal point, 0-100 (CSS object-position Y)


class PhotoFocus(BaseModel):
    x: int  # 0-100
    y: int  # 0-100


def _clamp_focus(value: int | None) -> int | None:
    """Keep a focal-point percentage inside 0-100, or drop it if absent."""
    if value is None:
        return None
    return max(0, min(100, int(value)))


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
    """Signed upload URL for the identity photo.

    The unlock rule is enforced HERE, before any signature is issued: replacing
    an existing photo requires the administration to have unlocked
    'photo_identite'. A replacement is STAGED to a separate pending path so the
    live photo is never overwritten before the administration validates it; the
    signature allows overwrite (upsert) because that pending path is stable. The
    very first photo (onboarding, no live photo yet) is written straight to the
    live path so the member card shows immediately."""
    membre_id, role = ctx
    etat = db.fetch_one(
        "SELECT photo_url, coalesce(champs_deverrouilles, '{}') AS deverrouilles FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    deja = bool((etat or {}).get("photo_url"))
    deverrouille = "photo_identite" in ((etat or {}).get("deverrouilles") or [])
    if deja and not deverrouille:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Le remplacement de la photo d'identité doit être débloqué par l'administration : ouvrez une demande « Changer ma photo d'identité ».",
        )
    path = f"{membre_id}/photo_pending.jpg" if deja else f"{membre_id}/photo.jpg"
    try:
        return storage.signed_upload_url(settings.storage_bucket_photos, path, upsert=deja)
    except storage.StorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable") from exc


@router.post("/photo/confirm", status_code=status.HTTP_204_NO_CONTENT)
def photo_confirm(payload: PhotoConfirm, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> None:
    """Record the uploaded identity photo.

    First upload (onboarding, no live photo): the photo becomes the live photo
    immediately so the member card shows. Replacement (requires an administration
    unlock of 'photo_identite'): the photo is STAGED as photo_pending, it does
    NOT overwrite the live photo, does NOT consume the unlock and does NOT submit
    anything. The staged photo travels with the single modification submission and
    is only promoted to the live photo when the administration validates it."""
    membre_id, role = ctx
    if not payload.path.startswith(f"{membre_id}/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    etat = db.fetch_one(
        "SELECT photo_url, coalesce(champs_deverrouilles, '{}') AS deverrouilles FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    deja = bool((etat or {}).get("photo_url"))
    deverrouille = "photo_identite" in ((etat or {}).get("deverrouilles") or [])
    if deja and not deverrouille:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Le remplacement de la photo d'identité doit être débloqué par l'administration : ouvrez une demande « Changer ma photo d'identité ».",
        )
    phash = _clean_phash(payload.phash)
    fx, fy = _clamp_focus(payload.focus_x), _clamp_focus(payload.focus_y)
    if deja:
        # Replacement: stage only (photo and its focal point). The single
        # modification submission and the admin validation put the new photo live.
        db.execute(
            "UPDATE membre SET photo_pending_url = %s, photo_pending_phash = %s, "
            "photo_pending_focus_x = %s, photo_pending_focus_y = %s WHERE id = %s",
            (payload.path, phash, fx, fy, membre_id),
            role=role,
        )
    else:
        # Onboarding first photo: goes live immediately, with its focal point.
        db.execute(
            "UPDATE membre SET photo_url = %s, photo_phash = %s, "
            "photo_focus_x = %s, photo_focus_y = %s WHERE id = %s",
            (payload.path, phash, fx, fy, membre_id),
            role=role,
        )


@router.patch("/photo/focus", status_code=status.HTTP_204_NO_CONTENT)
def photo_focus(payload: PhotoFocus, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> None:
    """Adjust the framing (focal point) of the member's current photo.

    This is display-only metadata (CSS object-position): it never changes the
    photo itself, so a member can re-centre the face of an already-validated
    photo at any time without a new administration validation."""
    membre_id, role = ctx
    fx, fy = _clamp_focus(payload.x), _clamp_focus(payload.y)
    db.execute(
        "UPDATE membre SET photo_focus_x = %s, photo_focus_y = %s WHERE id = %s",
        (fx, fy, membre_id),
        role=role,
    )


def _clean_phash(value: str | None) -> str | None:
    """Keep only a valid 16-hex-char difference hash; ignore anything else."""
    if not value:
        return None
    cleaned = value.strip().lower()
    if len(cleaned) == 16 and all(c in "0123456789abcdef" for c in cleaned):
        return cleaned
    return None


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


@router.get("/photo/pending")
def photo_pending_get(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str | None]:
    """Preview of a replacement photo staged but not yet validated. The member
    keeps seeing the live photo everywhere; this is the 'new photo pending' view."""
    membre_id, role = ctx
    row = db.fetch_one("SELECT photo_pending_url FROM membre WHERE id = %s", (membre_id,), role=role)
    path = row.get("photo_pending_url") if row else None
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


_EXT_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}


def _mime_for(path: str, declared: str | None) -> str:
    """A bucket-accepted content type for the encrypted object: reuse the declared
    mime, else infer from the extension (the private bucket rejects octet-stream)."""
    if declared and declared in _EXT_MIME.values():
        return declared
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_MIME.get(ext, "image/jpeg")


def _encrypt_document_at_rest(path: str, declared_mime: str | None) -> tuple[str, bool]:
    """Encrypt a just-uploaded plaintext object and return (stored_path, chiffre).

    Downloads the bytes with the service key, encrypts them (app-controlled key),
    writes the ciphertext to a fresh ``path.enc`` object, then deletes the
    plaintext. A distinct path avoids any overwrite ambiguity and guarantees the
    stored object is ciphertext.

    In production this is fail-closed on the most sensitive data: if any step
    fails, the plaintext is purged (best effort) and the confirmation is refused
    (503), so an identity document is never recorded or served in clear. Outside
    production the plaintext is left in place and (path, False) is returned so
    local development keeps working without the storage/crypto round-trip.
    """
    from . import crypto

    bucket = settings.storage_bucket_documents
    try:
        plaintext = storage.download_bytes(bucket, path)
        ciphertext = crypto.encrypt_bytes(plaintext)
        enc_path = f"{path}.enc"
        storage.upload_bytes(bucket, enc_path, ciphertext, content_type=_mime_for(path, declared_mime))
        storage.delete_object(bucket, path)
        return enc_path, True
    except (storage.StorageError, ValueError) as exc:
        if settings.is_production:
            try:
                storage.delete_object(bucket, path)
            except storage.StorageError:
                pass
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="document encryption unavailable, please retry",
            ) from exc
        return path, False


@router.post("/documents/confirm", status_code=status.HTTP_201_CREATED)
def doc_confirm(payload: DocConfirm, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str]:
    membre_id, role = ctx
    if payload.type not in DOC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown document type")
    if not payload.path.startswith(f"{membre_id}/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    # Encrypt the uploaded identity document at rest before recording it, so the
    # bytes in the bucket are ciphertext. Decryption happens only through an
    # authenticated, audited read.
    from . import crypto

    # Path of the object currently attached to this (member, type), if any, so it
    # can be purged from the bucket when the new upload replaces it (no orphan).
    ancien = db.fetch_one(
        "SELECT chemin_stockage FROM document WHERE membre_id = %s AND type = %s",
        (membre_id, payload.type),
        role=role,
    )
    stored_path, chiffre = _encrypt_document_at_rest(payload.path, payload.mime)
    algo = crypto.ALGO if chiffre else None
    # One logical document per (member, type): a new upload replaces the previous
    # one instead of piling up duplicates. This also means a valid document stays
    # attached across a correction cycle unless the member re-uploads that type.
    updated = db.execute(
        "UPDATE document SET statut = 'recu', bucket = %s, chemin_stockage = %s, nom_fichier = %s, mime = %s, "
        "chiffre = %s, chiffrement_algo = %s, recu_le = now() "
        "WHERE membre_id = %s AND type = %s RETURNING id",
        (settings.storage_bucket_documents, stored_path, payload.nom_fichier, payload.mime, chiffre, algo, membre_id, payload.type),
        role=role,
    )
    if updated:
        ancien_path = (ancien or {}).get("chemin_stockage")
        if ancien_path and str(ancien_path) != stored_path:
            storage.delete_object(settings.storage_bucket_documents, str(ancien_path))
        return {"id": str(updated["id"]), "chiffre": str(chiffre).lower()}
    created = db.execute(
        "INSERT INTO document (membre_id, type, statut, bucket, chemin_stockage, nom_fichier, mime, chiffre, chiffrement_algo, recu_le) "
        "VALUES (%s, %s, 'recu', %s, %s, %s, %s, %s, %s, now()) RETURNING id",
        (membre_id, payload.type, settings.storage_bucket_documents, stored_path, payload.nom_fichier, payload.mime, chiffre, algo),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="document not registered")
    return {"id": str(created["id"]), "chiffre": str(chiffre).lower()}


@router.get("/documents/{document_id}/url")
def doc_download_url(document_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, str | None]:
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT bucket, chemin_stockage, chiffre FROM document WHERE id = %s AND membre_id = %s",
        (document_id, membre_id),
        role=role,
    )
    if not row or not row.get("chemin_stockage"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    # An encrypted document is unreadable through a plain signed URL: point the
    # caller to the decrypting content endpoint instead.
    if row.get("chiffre"):
        return {"url": None, "chiffre": True, "content_path": f"/api/v1/membres/me/documents/{document_id}/content"}
    try:
        url = storage.signed_download_url(str(row["bucket"] or settings.storage_bucket_documents), str(row["chemin_stockage"]))
        return {"url": url, "chiffre": False}
    except storage.StorageError:
        return {"url": None, "chiffre": False}


def _document_bytes(row: dict[str, object]) -> bytes:
    """Return the plaintext bytes for a document row, decrypting when needed."""
    from . import crypto

    bucket = str(row.get("bucket") or settings.storage_bucket_documents)
    raw = storage.download_bytes(bucket, str(row["chemin_stockage"]))
    if row.get("chiffre"):
        raw = crypto.decrypt_bytes(raw)
    return raw


def _safe_media_type(path: str) -> str:
    """A content type derived from the real file extension, NEVER the client mime.

    The client-declared mime is untrusted: a member could upload an HTML file
    tagged text/html and get it executed in the API origin when a staff member
    opens it. We only ever serve a known image/PDF type; anything else falls back
    to octet-stream so the browser downloads it instead of rendering it. The
    stored ciphertext path ends in ``.enc``: strip it to read the real extension.
    """
    real = path[:-4] if path.lower().endswith(".enc") else path
    ext = real.rsplit(".", 1)[-1].lower() if "." in real else ""
    return _EXT_MIME.get(ext, "application/octet-stream")


def _document_response(data: bytes, path: str) -> Response:
    """Serve a member document defensively: a safe extension-derived content type,
    forced download, and a locked-down CSP so no markup/script can execute in the
    API origin even if a hostile file slips through."""
    return Response(
        content=data,
        media_type=_safe_media_type(path),
        headers={
            "Content-Disposition": "attachment",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/documents/{document_id}/content")
def doc_content(document_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> Response:
    """Decrypted bytes of the member's own document (automatic decryption)."""
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT bucket, chemin_stockage, mime, chiffre FROM document WHERE id = %s AND membre_id = %s",
        (document_id, membre_id),
        role=role,
    )
    if not row or not row.get("chemin_stockage"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    try:
        data = _document_bytes(row)
    except (storage.StorageError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="document unreadable") from exc
    return _document_response(data, str(row["chemin_stockage"]))


# Identity documents are decrypted here: keep this to the roles with a real need
# (never controleur, a field scan operator, nor direction, read-only supervision).

admin_router = APIRouter(prefix="/api/v1/admin", tags=["fichiers"])


@admin_router.get("/documents/{document_id}/url")
def admin_doc_url(document_id: str, user: Annotated[UserMe, Depends(require_permission("documents.gerer"))]) -> dict[str, str | bool | None]:
    """Signed download URL for any member's document, for admin review (e.g. a
    hand-signed attestation scan). Encrypted documents are read through the
    audited content endpoint instead of a plain signed URL."""
    row = db.fetch_one(
        "SELECT bucket, chemin_stockage, chiffre FROM document WHERE id = %s",
        (document_id,),
        role=None,
    )
    if not row or not row.get("chemin_stockage"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    if row.get("chiffre"):
        return {"url": None, "chiffre": True, "content_path": f"/api/v1/admin/documents/{document_id}/content"}
    try:
        url = storage.signed_download_url(str(row["bucket"] or settings.storage_bucket_documents), str(row["chemin_stockage"]))
        return {"url": url, "chiffre": False}
    except storage.StorageError:
        return {"url": None, "chiffre": False}


@admin_router.get("/documents/{document_id}/content")
def admin_doc_content(document_id: str, user: Annotated[UserMe, Depends(require_permission("documents.gerer"))]) -> Response:
    """Decrypted bytes of any member's document, for staff review. Every access is
    audited (who read which document), because this is a controlled decryption of
    a sensitive identity file."""
    row = db.fetch_one(
        "SELECT membre_id, bucket, chemin_stockage, mime, chiffre FROM document WHERE id = %s",
        (document_id,),
        role=None,
    )
    if not row or not row.get("chemin_stockage"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    try:
        data = _document_bytes(row)
    except (storage.StorageError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="document unreadable") from exc
    audit.log(user.id, user.role, "consultation_document", "document", document_id,
              {"membre_id": str(row.get("membre_id")), "chiffre": bool(row.get("chiffre"))})
    return _document_response(data, str(row["chemin_stockage"]))


@admin_router.get("/membres/{membre_id}/photo-url")
def admin_photo_url(membre_id: str, user: Annotated[UserMe, Depends(require_permission("membres.consulter"))]) -> dict[str, str | None]:
    """Signed download URL for any member's identity photo, for staff views
    (registration review, QR-scan identity card, member directory). Reads as an
    owner query since the endpoint is already gated by staff roles."""
    row = db.fetch_one("SELECT photo_url FROM membre WHERE id = %s", (membre_id,), role=None)
    path = row.get("photo_url") if row else None
    if not path:
        return {"url": None}
    try:
        return {"url": storage.signed_download_url(settings.storage_bucket_photos, str(path))}
    except storage.StorageError:
        return {"url": None}
