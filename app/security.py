"""Password verification (Argon2) and JWT issuance and decoding."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import settings

_hasher = PasswordHasher()
# A constant Argon2 hash to verify against when an account does not exist, so a
# failed login takes the same time whether the e-mail is known or not (defeats
# timing-based account enumeration).
_DUMMY_HASH = _hasher.hash("adsum-timing-equaliser")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    """Verify against the real hash, or against a constant dummy hash when the
    account is absent. Always runs one Argon2 verification, equalising timing."""
    return verify_password(password, hashed) if hashed else (verify_password(password, _DUMMY_HASH) and False)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def create_access_token(subject: str, role: str, sid: str | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
        "iss": "adsum-api",
    }
    if sid:
        payload["sid"] = sid  # session id, so logout can close the exact session
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["sub", "role", "exp"]},
        issuer="adsum-api",
    )
