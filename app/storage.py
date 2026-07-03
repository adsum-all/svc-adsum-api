"""Supabase Storage (S3) access for member files.

Files (profile photos, identity documents, signed consents) live in PRIVATE
buckets. The browser never holds the service key: the API mints short-lived
signed upload URLs (the client PUTs the file straight to Supabase, which keeps
large bodies off the serverless function) and short-lived signed download URLs.
Deletion supports the RGPD right to erasure. Uses the standard library only,
because httpx is unreliable on the Vercel Python runtime.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.request

from .config import settings


class StorageError(RuntimeError):
    pass


def _request(
    method: str, path: str, body: dict[str, object] | None = None, headers: dict[str, str] | None = None
) -> dict[str, object]:
    if not settings.supabase_url or not settings.supabase_service_key:
        raise StorageError("storage is not configured")
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1{path}"
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {settings.supabase_service_key}")
    req.add_header("apikey", settings.supabase_service_key)
    for cle, valeur in (headers or {}).items():
        req.add_header(cle, valeur)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted Supabase URL)
            raw = resp.read()
            return _json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error path
        raise StorageError(f"storage {method} {path} failed: {exc.code}") from exc


def signed_upload_url(bucket: str, path: str, upsert: bool = False) -> dict[str, str]:
    """Return a one-time signed URL the client uses to PUT the file directly.

    With ``upsert`` the signature allows overwriting an existing object (needed
    when replacing an identity photo at its stable path); without it, Supabase
    refuses to sign an upload towards an object that already exists.
    """
    headers = {"x-upsert": "true"} if upsert else None
    res = _request("POST", f"/object/upload/sign/{bucket}/{path}", headers=headers)
    token = str(res.get("url", ""))
    return {
        "bucket": bucket,
        "path": path,
        "upload_url": f"{settings.supabase_url.rstrip('/')}/storage/v1{token}",
    }


def signed_download_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Return a short-lived signed URL to GET a private object."""
    res = _request("POST", f"/object/sign/{bucket}/{path}", {"expiresIn": expires_in})
    signed = str(res.get("signedURL") or res.get("signedUrl") or "")
    if not signed:
        raise StorageError("no signed url returned")
    return f"{settings.supabase_url.rstrip('/')}/storage/v1{signed}"


def upload_bytes(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    """Upload raw bytes with the service key (server-side).

    Used for encrypted objects: the API holds the ciphertext and pushes it to the
    private bucket itself, so the plaintext never lands in storage.
    """
    if not settings.supabase_url or not settings.supabase_service_key:
        raise StorageError("storage is not configured")
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {settings.supabase_service_key}")
    req.add_header("apikey", settings.supabase_service_key)
    req.add_header("Content-Type", content_type)
    req.add_header("x-upsert", "true")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted Supabase URL)
            resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error path
        raise StorageError(f"storage upload {bucket}/{path} failed: {exc.code}") from exc


def download_bytes(bucket: str, path: str) -> bytes:
    """Download raw bytes with the service key (server-side), for decryption."""
    if not settings.supabase_url or not settings.supabase_service_key:
        raise StorageError("storage is not configured")
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {settings.supabase_service_key}")
    req.add_header("apikey", settings.supabase_service_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted Supabase URL)
            return resp.read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error path
        raise StorageError(f"storage download {bucket}/{path} failed: {exc.code}") from exc


def delete_object(bucket: str, path: str) -> None:
    """Delete an object (RGPD erasure). Missing objects are ignored."""
    try:
        _request("DELETE", f"/object/{bucket}/{path}")
    except StorageError:
        pass


def delete_prefix(bucket: str, prefix: str) -> None:
    """Delete every object under a member's folder (RGPD erasure)."""
    try:
        listing = _request("POST", f"/object/list/{bucket}", {"prefix": prefix, "limit": 100})
    except StorageError:
        return
    names = [str(o.get("name")) for o in listing] if isinstance(listing, list) else []
    for name in names:
        delete_object(bucket, f"{prefix}{name}")
