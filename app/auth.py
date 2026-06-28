"""Authentication endpoints: real login and current user."""
from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .schemas import LoginRequest, TokenResponse, UserMe
from .security import create_access_token, decode_access_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=True)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    user = db.get_user_by_email(payload.email)
    if not user or not user["actif"] or not verify_password(payload.password, user["hash_mdp"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = create_access_token(subject=str(user["id"]), role=user["role"])
    return TokenResponse(access_token=token, role=user["role"])


def current_user(creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)]) -> UserMe:
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token") from exc
    # The role from the verified token drives the RLS session variable.
    user = db.get_user_by_id(claims["sub"], role=claims["role"])
    if not user or not user["actif"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account not available")
    return UserMe(
        id=str(user["id"]),
        email=user["email"],
        role=user["role"],
        membre_id=str(user["membre_id"]) if user["membre_id"] else None,
    )


@router.get("/me", response_model=UserMe)
def me(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
    return user
