"""Unit tests for the QR token signing module (no database needed)."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import qr
from app.config import settings


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _set_key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    seed_b64 = base64.urlsafe_b64encode(key.private_bytes_raw()).decode("ascii")
    monkeypatch.setattr(settings, "qr_signing_key", seed_b64)
    return key


def test_issue_token_is_signed_and_verifiable(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _set_key(monkeypatch)
    monkeypatch.setattr(settings, "qr_key_version", 3)
    membre_id = "11111111-1111-1111-1111-111111111111"

    issued = qr.issue_token(membre_id, ttl_seconds=60)
    prefix, payload_b64, sig_b64 = str(issued["token"]).split(".")

    assert prefix == "ADSUM1"
    payload = json.loads(_b64u_decode(payload_b64))
    assert payload["m"] == membre_id
    assert payload["kv"] == 3
    assert payload["exp"] - payload["iat"] == 60

    # The signature must verify against the public key, with no exception raised.
    key.public_key().verify(_b64u_decode(sig_b64), f"ADSUM1.{payload_b64}".encode("ascii"))


def test_issue_token_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "qr_signing_key", "")
    with pytest.raises(qr.QrSigningUnavailable):
        qr.issue_token("any-id")


def test_issue_token_rejects_wrong_key_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "qr_signing_key", base64.urlsafe_b64encode(b"too-short").decode())
    with pytest.raises(qr.QrSigningUnavailable):
        qr.issue_token("any-id")


def test_public_key_b64_matches_private(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _set_key(monkeypatch)
    assert _b64u_decode(qr.public_key_b64()) == key.public_key().public_bytes_raw()


def test_verify_token_accepts_a_fresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    membre_id = "22222222-2222-2222-2222-222222222222"
    issued = qr.issue_token(membre_id, ttl_seconds=60)

    result = qr.verify_token(str(issued["token"]))

    assert result["valid"] is True
    assert result["membre_id"] == membre_id
    assert result["expires_at"] == issued["expires_at"]


def test_verify_token_rejects_an_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    issued = qr.issue_token("33333333-3333-3333-3333-333333333333", ttl_seconds=-1)

    result = qr.verify_token(str(issued["token"]))

    assert result["valid"] is False
    assert result["reason"] == "expired"


def test_verify_token_rejects_a_tampered_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    issued = qr.issue_token("44444444-4444-4444-4444-444444444444", ttl_seconds=60)
    prefix, payload_b64, sig_b64 = str(issued["token"]).split(".")
    # Flip the first character of the signature to a different base64url char.
    altered = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]

    result = qr.verify_token(f"{prefix}.{payload_b64}.{altered}")

    assert result["valid"] is False
    assert result["reason"] == "invalid signature"


def test_verify_token_rejects_a_malformed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    assert qr.verify_token("not-a-token")["valid"] is False
    assert qr.verify_token("ADSUM1.only-two-parts")["valid"] is False
