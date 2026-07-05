"""Application-level encryption for identity documents at rest.

Identity documents are the most sensitive files we hold. On top of the private
bucket and short-lived signed URLs, their bytes are encrypted with a key the API
controls (Fernet, AES-128-CBC + HMAC), so a leak of the storage bucket or the
database alone never exposes a readable document. Decryption happens only through
an authenticated, audited endpoint.

Key management and rotation
---------------------------
The active key comes from ``ADSUM_DOC_ENCRYPTION_KEY`` (a 44-char Fernet key). It
is stored outside the repository and outside the database (Vercel encrypted
environment variable), with a break-glass copy in Supabase Vault and an offline
copy in the project ``.secret`` folder.

Rotation is zero-downtime via ``MultiFernet``: new data is always encrypted with
the primary key, while decryption accepts the primary key OR any retired key
listed (comma-separated) in ``ADSUM_DOC_ENCRYPTION_KEYS_OLD``. To rotate: generate
a new key, set it as the primary and move the previous one into
``..._KEYS_OLD``, redeploy, then run ``scripts/rotate_doc_key.py`` to re-encrypt
existing documents with the new primary. Once every document is re-encrypted the
old key can be dropped from ``..._KEYS_OLD``.

In production (``ADSUM_ENVIRONMENT=production``) a dedicated key is mandatory:
if it is missing, the API fails closed rather than silently deriving one from
the JWT secret, which would tie document confidentiality to the token secret and
break every document the day the JWT secret is rotated. Outside production the
key may be derived from the JWT secret so local development still works.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import settings

ALGO = "fernet-v1"


def _key_from(raw: str) -> Fernet:
    """A Fernet from a ready-made key, else derived from the JWT secret.

    In production the derived fallback is refused: a missing or malformed
    dedicated key raises ``RuntimeError`` so documents are never encrypted with
    a JWT-derived key. Outside production the derivation keeps local dev working.
    """
    if raw:
        try:
            return Fernet(raw.encode())
        except (ValueError, TypeError):
            if settings.is_production:
                raise RuntimeError(
                    "ADSUM_DOC_ENCRYPTION_KEY is set but is not a valid Fernet key in production"
                ) from None
    elif settings.is_production:
        raise RuntimeError(
            "ADSUM_DOC_ENCRYPTION_KEY must be set in production (no JWT-derived fallback allowed)"
        )
    seed = (raw or settings.jwt_secret or "adsum").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


@lru_cache(maxsize=1)
def _primary() -> Fernet:
    """The key new ciphertext is encrypted with."""
    return _key_from(settings.doc_encryption_key or "")


@lru_cache(maxsize=1)
def _multi() -> MultiFernet:
    """Primary key first (used for encryption), then any retired keys, so a
    ciphertext produced before a rotation still decrypts."""
    keys = [_primary()]
    for old in (settings.doc_encryption_keys_old or "").split(","):
        old = old.strip()
        if old:
            try:
                keys.append(Fernet(old.encode()))
            except (ValueError, TypeError):
                continue
    return MultiFernet(keys)


def encrypt_bytes(data: bytes) -> bytes:
    """Return the ciphertext for ``data`` using the current primary key."""
    return _primary().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Return the plaintext for a stored ciphertext (primary or a retired key).

    Raises ``ValueError`` when the ciphertext is not valid for any known key, so
    callers return a clean error instead of leaking a crypto exception.
    """
    try:
        return _multi().decrypt(token)
    except InvalidToken as exc:
        raise ValueError("document could not be decrypted") from exc


def rotate_token(token: bytes) -> bytes:
    """Re-encrypt an existing ciphertext with the current primary key, without
    exposing the plaintext (used by the rotation script)."""
    return _multi().rotate(token)
