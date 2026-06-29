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
import binascii
import json
import time
from typing import TYPE_CHECKING

from .config import settings

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

PREFIX = "ADSUM1"


class QrSigningUnavailable(RuntimeError):
    """Raised when QR signing cannot be performed (no key, or library missing)."""


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def _load_private_key() -> Ed25519PrivateKey:
    # Imported lazily so a missing cryptography wheel degrades only the QR
    # endpoint (HTTP 503) instead of breaking the whole API at import time.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise QrSigningUnavailable("the cryptography library is not available") from exc
    seed_b64 = settings.qr_signing_key.strip()
    if not seed_b64:
        raise QrSigningUnavailable("ADSUM_QR_SIGNING_KEY is not configured")
    padded = seed_b64 + "=" * (-len(seed_b64) % 4)
    try:
        seed = base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error) as exc:
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


def _load_public_key() -> Ed25519PublicKey:
    # Use the configured public key when provided (verify-only deployment),
    # otherwise derive it from the private signing seed.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise QrSigningUnavailable("the cryptography library is not available") from exc
    pub_b64 = settings.qr_public_key.strip()
    if pub_b64:
        try:
            raw = _b64u_decode(pub_b64)
        except (ValueError, binascii.Error) as exc:
            raise QrSigningUnavailable("ADSUM_QR_PUBLIC_KEY is not valid base64") from exc
        if len(raw) != 32:
            raise QrSigningUnavailable("ADSUM_QR_PUBLIC_KEY must decode to 32 bytes")
        return Ed25519PublicKey.from_public_bytes(raw)
    return _load_private_key().public_key()


def verify_token(token: str) -> dict[str, object]:
    """Verify a QR token offline and return its claims.

    Parses the ``ADSUM1.<payload>.<signature>`` format, checks the Ed25519
    signature against the configured public key and the expiry, then returns
    ``{"valid": bool, "reason"?: str, "membre_id"?, "issued_at"?, "expires_at"?,
    "key_version"?}``. A malformed token or a signing key error yields
    ``{"valid": False, "reason": ...}`` rather than raising.
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        return {"valid": False, "reason": "malformed token"}
    _, payload_b64, sig_b64 = parts
    try:
        public_key = _load_public_key()
    except QrSigningUnavailable as exc:
        return {"valid": False, "reason": str(exc)}
    from cryptography.exceptions import InvalidSignature

    signing_input = f"{PREFIX}.{payload_b64}".encode("ascii")
    try:
        public_key.verify(_b64u_decode(sig_b64), signing_input)
    except (ValueError, binascii.Error):
        return {"valid": False, "reason": "malformed signature"}
    except InvalidSignature:
        return {"valid": False, "reason": "invalid signature"}
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, binascii.Error):
        return {"valid": False, "reason": "malformed payload"}
    exp = payload.get("exp")
    iat = payload.get("iat")
    membre_id = payload.get("m")
    if not isinstance(exp, int) or not isinstance(iat, int) or not isinstance(membre_id, str):
        return {"valid": False, "reason": "malformed payload"}
    if int(time.time()) > exp:
        return {
            "valid": False,
            "reason": "expired",
            "membre_id": membre_id,
            "issued_at": iat,
            "expires_at": exp,
            "key_version": payload.get("kv"),
        }
    return {
        "valid": True,
        "membre_id": membre_id,
        "issued_at": iat,
        "expires_at": exp,
        "key_version": payload.get("kv"),
    }
