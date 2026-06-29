"""Server-side signing of member QR check-in tokens.

A QR token is a compact, offline-verifiable string a member shows for check-in:

    ADSUM1.<payload_b64url>.<signature_b64url>

where ``payload`` is the JSON object ``{"v": 1, "m": <membre_id>, "iat": <epoch>,
"exp": <epoch>, "kv": <key_version>}`` and ``signature`` is the Ed25519 signature
of the ASCII bytes ``ADSUM1.<payload_b64url>``. A controller terminal verifies it
with the published public key, with no network call (ADR QR offline check).

The Ed25519 private seed is read from the environment only (Constitution I10),
never hardcoded. When it is absent, signing raises ``QrSigningUnavailable`` and
the API surfaces HTTP 503 instead of issuing an unsigned token.
"""
from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import settings

PREFIX = "ADSUM1"


class QrSigningUnavailable(RuntimeError):
    """Raised when no QR signing key is configured in the environment."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_private_key() -> Ed25519PrivateKey:
    seed_b64 = settings.qr_signing_key.strip()
    if not seed_b64:
        raise QrSigningUnavailable("ADSUM_QR_SIGNING_KEY is not configured")
    padded = seed_b64 + "=" * (-len(seed_b64) % 4)
    try:
        seed = base64.urlsafe_b64decode(padded)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise QrSigningUnavailable("ADSUM_QR_SIGNING_KEY is not valid base64") from exc
    if len(seed) != 32:
        raise QrSigningUnavailable("ADSUM_QR_SIGNING_KEY must decode to 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def issue_token(membre_id: str, ttl_seconds: int | None = None) -> dict[str, object]:
    """Issue a signed QR token for ``membre_id``.

    Returns a dict with the token string and its timing and key metadata. Raises
    ``QrSigningUnavailable`` when the signing key is missing or malformed.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.qr_ttl_seconds
    key = _load_private_key()
    iat = int(time.time())
    exp = iat + ttl
    payload = {
        "v": 1,
        "m": membre_id,
        "iat": iat,
        "exp": exp,
        "kv": settings.qr_key_version,
    }
    payload_b64 = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{PREFIX}.{payload_b64}".encode("ascii")
    signature = key.sign(signing_input)
    token = f"{PREFIX}.{payload_b64}.{_b64u_encode(signature)}"
    return {
        "token": token,
        "membre_id": membre_id,
        "issued_at": iat,
        "expires_at": exp,
        "key_version": settings.qr_key_version,
    }


def public_key_b64() -> str:
    """Return the base64url Ed25519 public key, for terminals to verify tokens."""
    raw = _load_private_key().public_key().public_bytes_raw()
    return _b64u_encode(raw)
