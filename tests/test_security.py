"""Unit tests for password hashing and JWT (no database, run in CI)."""
from __future__ import annotations

import jwt
import pytest

from app.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_and_verify() -> None:
    hashed = hash_password("Str0ng-Passw0rd!")
    assert hashed != "Str0ng-Passw0rd!"
    assert verify_password("Str0ng-Passw0rd!", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_jwt_roundtrip_carries_subject_and_role() -> None:
    token = create_access_token(subject="user-123", role="super_admin")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"
    assert claims["role"] == "super_admin"
    assert claims["iss"] == "adsum-api"


def test_jwt_rejects_tampered_token() -> None:
    token = create_access_token(subject="user-123", role="admin")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token + "x")
