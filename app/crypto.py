"""Application-level encryption for identity documents at rest.

Identity documents are the most sensitive files we hold. On top of the private
bucket and short-lived signed URLs, their bytes are encrypted with a key the API
controls (Fernet, AES-128-CBC + HMAC), so a leak of the storage bucket or the
database alone never exposes a readable document. Decryption happens only through
an authenticated, audited endpoint.

The key comes from ``ADSUM_DOC_ENCRYPTION_KEY``. If it is a valid Fernet key it is
used directly; otherwise (or if unset) a key is derived from it, or from the JWT
secret, so encryption always works. Use a dedicated key in production.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

ALGO = "fernet-v1"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    raw = settings.doc_encryption_key or ""
    if raw:
        try:
            # A ready-made Fernet key (44-char url-safe base64) is used as-is.
            return Fernet(raw.encode())
        except (ValueError, TypeError):
            pass
    seed = (raw or settings.jwt_secret or "adsum").encode()
    derived = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(derived)


def encrypt_bytes(data: bytes) -> bytes:
    """Return the ciphertext for ``data`` (bytes stored in the bucket)."""
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Return the plaintext for a stored ciphertext.

    Raises ``ValueError`` when the ciphertext is not valid for the current key,
    so callers can return a clean error instead of leaking a crypto exception.
    """
    try:
        return _fernet().decrypt(token)
    except InvalidToken as exc:
        raise ValueError("document could not be decrypted") from exc
