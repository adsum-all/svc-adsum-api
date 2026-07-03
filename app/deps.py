"""Reusable authorization dependencies built on the authenticated user."""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status

from .auth import current_user
from .schemas import UserMe


def require_roles(*roles: str) -> Callable[[UserMe], UserMe]:
    """Build a dependency that allows only the listed roles.

    The returned dependency reads the authenticated user and raises HTTP 403
    when ``user.role`` is not in ``roles``. The role comes from the verified JWT
    and also drives the RLS session variable, so this is defense in depth on top
    of the database policies.
    """
    allowed = frozenset(roles)

    def guard(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )
        return user

    return guard
