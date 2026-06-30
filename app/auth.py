"""Authentication endpoints: real login and current user."""
from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from . import db
from .email_gateway import send_code, verify_code
from .schemas import LoginRequest, TokenResponse, UserMe
from .security import create_access_token, decode_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=True)

_ALLOWED_PURPOSES = {"login_2fa", "password_reset", "engagement"}


class OtpRequest(BaseModel):
    email: EmailStr
    purpose: str = "login_2fa"


class OtpVerify(BaseModel):
    email: EmailStr
    purpose: str = "login_2fa"
    code: str


class ResetRequest(BaseModel):
    email: EmailStr
    code: str
    nouveau: str


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


@router.post("/request-otp")
def request_otp(payload: OtpRequest) -> dict[str, object]:
    """Send a one-time code by e-mail for 2FA, password reset or signature.

    Always returns ok (does not reveal whether the e-mail exists), so it cannot
    be used to enumerate accounts.
    """
    if payload.purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown purpose")
    sent, provider = send_code(str(payload.email), payload.purpose)
    return {"ok": True, "sent": sent, "provider": provider}


@router.post("/verify-otp")
def verify_otp(payload: OtpVerify) -> dict[str, object]:
    return {"valid": verify_code(str(payload.email), payload.purpose, payload.code)}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(payload: ResetRequest) -> None:
    """Set a new password after a valid password_reset code, self-service."""
    if len(payload.nouveau) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short")
    if not verify_code(str(payload.email), "password_reset", payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    # Owner connection (role=None) bypasses RLS; scoped by e-mail. No reveal if absent.
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s WHERE email = %s",
        (hash_password(payload.nouveau), str(payload.email)),
    )
