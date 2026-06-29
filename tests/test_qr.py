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
