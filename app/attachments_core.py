"""Shared, hardened primitives for file attachments.

Both event attachments (``evenement_piece``) and collaboration card/comment
attachments (``collab_piece``) need the same security decisions:

- refuse active-content types (HTML, SVG, XML) that could execute in the browser;
- only serve an allowlist of types inline, everything else as a download;
- decode data URLs safely with a size cap;
- serve objects through short-lived signed URLs when a private bucket is configured,
  or keep an inline data URL otherwise (backward compatible, no bucket required).

Table-specific INSERT/SELECT/DELETE stays in each caller; this module owns only the
security-critical helpers so the policy is defined once.
"""
from __future__ import annotations

import base64

from fastapi import HTTPException, status

from . import storage
from .config import settings

# Cap the request body: ~4/3 base64 overhead, so 3.5 MB of data URL is ~2.5 MB file.
MAX_DATA_URL = 3_500_000
# Signed download URLs are minted per read; an hour covers a page view.
SIGNED_URL_TTL = 3600

# Types allowed to render inline (none can carry active content). Everything else is
# served as an attachment download.
INLINE_SAFE_TYPES = frozenset(
    {
        "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp",
        "application/pdf", "text/plain",
    }
)
# Active-content types are refused outright so a script-bearing file is never stored.
DANGEROUS_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "image/svg+xml", "application/xml", "text/xml"}
)


def bucket() -> str:
    """Configured private bucket for attachments, or "" when inline mode is in force."""
    if settings.storage_bucket_evenements and settings.supabase_url and settings.supabase_service_key:
        return settings.storage_bucket_evenements
    return ""


def data_url_mime(data_url: str) -> str:
    """Declared MIME type of a data URL, without decoding the payload."""
    header = data_url.split(",", 1)[0]
    return header[5:].split(";", 1)[0].strip().lower() if header.startswith("data:") else ""


def reject_active_content(mime: str, declared: str) -> None:
    """Refuse attachments whose type can execute script (HTML, SVG, XML)."""
    for candidate in (mime, declared):
        if (candidate or "").split(";", 1)[0].strip().lower() in DANGEROUS_TYPES:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="type de fichier non autorisé (contenu potentiellement actif)",
            )


def stored_content_type(mime: str, declared: str) -> str:
    """Content type to store and serve: the real one when it is on the inline
    allowlist, else a neutral octet-stream (which is served as a download)."""
    effective = (mime or declared or "").split(";", 1)[0].strip().lower()
    return effective if effective in INLINE_SAFE_TYPES else "application/octet-stream"


def decode_data_url(data_url: str) -> bytes:
    """Return the raw bytes of a ``data:<mime>;base64,<payload>`` URL."""
    payload = data_url.split(",", 1)[1] if "," in data_url else ""
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError subclass
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="pièce invalide (encodage base64)"
        ) from exc


def validate_payload(data_url: str, declared_type: str) -> None:
    """Validate a data URL upload: shape, size cap and active-content refusal."""
    if not data_url or not data_url.startswith("data:"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="pièce invalide (data URL attendue)")
    if len(data_url) > MAX_DATA_URL:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="fichier trop volumineux (max ~2,5 Mo)"
        )
    reject_active_content(data_url_mime(data_url), declared_type)


def signed_url(storage_path: str, nom: str, type_: str) -> str:
    """Signed URL for a stored object; forces an attachment download for non-inline
    types. Degrades to an empty string on a storage failure rather than raising."""
    download = None if (type_ or "") in INLINE_SAFE_TYPES else (nom or "piece")
    try:
        return storage.signed_download_url(bucket(), storage_path, SIGNED_URL_TTL, download=download)
    except (storage.StorageError, OSError):  # URLError (DNS/connection) subclasses OSError
        return ""


def served_url(url: str | None, storage_path: str | None, nom: str, type_: str) -> str:
    """Resolve the URL a client can fetch: a signed URL for object-backed rows, or
    the stored inline data URL otherwise."""
    if storage_path:
        return signed_url(str(storage_path), nom, type_)
    return url or ""
