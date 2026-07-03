"""Brute-force rate limiting for sensitive auth endpoints.

A sliding window counts recent attempts from the caller IP on a given endpoint,
backed by the auth_attempt table so it works on a stateless serverless runtime.
It fails open: a database hiccup must never lock users out of authentication.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from . import db

# endpoint -> (max attempts, window in seconds)
_LIMITS = {
    "login": (10, 300),
    "premiere-connexion": (10, 300),
    "request-otp": (5, 300),
    "reset-password": (10, 300),
}


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def enforce(request: Request, endpoint: str) -> None:
    """Raise 429 if the IP exceeded the endpoint's window, then record the hit."""
    limit = _LIMITS.get(endpoint)
    if not limit:
        return
    max_attempts, window = limit
    ip = _client_ip(request)
    try:
        if ip:
            row = db.fetch_one(
                "SELECT count(*) AS n FROM auth_attempt "
                "WHERE ip = %s::inet AND endpoint = %s AND cree_le > now() - (%s || ' seconds')::interval",
                (ip, endpoint, str(window)),
            )
            if row and int(row["n"]) >= max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="too many attempts, please wait a few minutes",
                )
        db.execute("INSERT INTO auth_attempt (ip, endpoint) VALUES (%s::inet, %s)", (ip, endpoint))
        # Opportunistic cleanup keeps the table small without a scheduled job.
        db.execute("DELETE FROM auth_attempt WHERE cree_le < now() - interval '1 hour'", ())
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - rate limiting must never break authentication
        return
