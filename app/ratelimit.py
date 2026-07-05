"""Brute-force rate limiting for sensitive auth endpoints.

A sliding window counts recent attempts from the caller IP on a given endpoint,
backed by the auth_attempt table so it works on a stateless serverless runtime.
It fails open: a database hiccup must never lock users out of authentication.
"""
# ruff: noqa: E501
from __future__ import annotations

from fastapi import HTTPException, Request, status

from . import db
from .clientip import client_ip

# endpoint -> (max attempts, window in seconds)
_LIMITS = {
    "login": (10, 300),
    "premiere-connexion": (10, 300),
    "request-otp": (5, 300),
    "reset-password": (10, 300),
    # OTP verification is the sensitive oracle: keep it tight per IP, on top of
    # the per-email failure lockout below.
    "verify-otp": (10, 300),
}

# One-time-code guessing lockout per account, independent of the IP so it cannot
# be defeated by rotating IPs: after this many failed OTP attempts for an
# (email, purpose) within the window, verification is refused.
_OTP_MAX_FAILURES = 6
_OTP_WINDOW = 900

_TOO_MANY = HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too many attempts, please wait a few minutes")


def enforce(request: Request, endpoint: str) -> None:
    """Raise 429 if the trusted IP exceeded the endpoint's window, then record it."""
    limit = _LIMITS.get(endpoint)
    if not limit:
        return
    max_attempts, window = limit
    ip = client_ip(request)
    try:
        if ip:
            row = db.fetch_one(
                "SELECT count(*) AS n FROM auth_attempt "
                "WHERE ip = %s::inet AND endpoint = %s AND cree_le > now() - (%s || ' seconds')::interval",
                (ip, endpoint, str(window)),
            )
            if row and int(row["n"]) >= max_attempts:
                raise _TOO_MANY
        db.execute("INSERT INTO auth_attempt (ip, endpoint) VALUES (%s::inet, %s)", (ip, endpoint))
        # Opportunistic cleanup keeps the table small without a scheduled job.
        db.execute("DELETE FROM auth_attempt WHERE cree_le < now() - interval '1 hour'", ())
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - rate limiting must never break authentication
        return


def _otp_key(email: str, purpose: str) -> str:
    return f"otp-fail:{purpose}:{email.strip().lower()}"


def otp_guard(email: str, purpose: str) -> None:
    """Refuse OTP verification once an (email, purpose) has too many recent
    failures, independent of the caller IP. Raises 429 when locked out."""
    key = _otp_key(email, purpose)
    try:
        row = db.fetch_one(
            "SELECT count(*) AS n FROM auth_attempt WHERE endpoint = %s AND cree_le > now() - (%s || ' seconds')::interval",
            (key, str(_OTP_WINDOW)),
        )
    except Exception:  # noqa: BLE001 - lockout lookup must never break auth
        return
    if row and int(row["n"]) >= _OTP_MAX_FAILURES:
        raise _TOO_MANY


def otp_failure(email: str, purpose: str) -> None:
    """Record one failed OTP verification for the lockout counter."""
    try:
        db.execute("INSERT INTO auth_attempt (ip, endpoint) VALUES (NULL, %s)", (_otp_key(email, purpose),))
    except Exception:  # noqa: BLE001 - best effort
        return


def throttle_action(key: str, max_in_window: int, window: int) -> bool:
    """Generic per-key throttle, IP-independent, backed by the auth_attempt table.

    Returns True when the action is allowed (and records it), False when the
    window is already saturated. Used to bound expensive fan-out actions (mass
    notifications, a paid channel like WhatsApp) so a privileged endpoint cannot
    be looped to amplify sends or run up cost. Fails open on a DB error so a
    legitimate action is never blocked by an infrastructure hiccup.
    """
    try:
        row = db.fetch_one(
            "SELECT count(*) AS n FROM auth_attempt WHERE endpoint = %s AND cree_le > now() - (%s || ' seconds')::interval",
            (key, str(window)),
        )
        if row and int(row["n"]) >= max_in_window:
            return False
        db.execute("INSERT INTO auth_attempt (ip, endpoint) VALUES (NULL, %s)", (key,))
        return True
    except Exception:  # noqa: BLE001 - a throttle must never break the action on a DB hiccup
        return True
