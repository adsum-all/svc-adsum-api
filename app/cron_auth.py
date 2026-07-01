"""Shared authentication for the scheduled cron endpoints.

Vercel injects ``Authorization: Bearer $CRON_SECRET`` on scheduled invocations.
The check is fail-closed: if no secret is configured the endpoint refuses to run
rather than executing unauthenticated, and the comparison is constant-time to
avoid leaking the secret through timing.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, status

from .config import settings


def require_cron_auth(authorization: str | None) -> None:
    """Reject unless the caller presents the configured CRON_SECRET bearer.

    Raises 503 when no secret is configured (never runs open) and 401 on a
    missing or mismatched bearer.
    """
    secret = os.environ.get("CRON_SECRET") or settings.cron_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cron secret not configured",
        )
    expected = f"Bearer {secret}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
