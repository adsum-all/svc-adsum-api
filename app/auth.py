"""Authentication endpoints: real login and current user."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from . import db, ratelimit
from .schemas import LoginRequest, TokenResponse, UserMe
from .security import create_access_token, decode_access_token, hash_password, verify_password

try:
    from .email_gateway import send_code, verify_code
except Exception:  # keep the API up even if the e-mail gateway fails to import

    def send_code(email: str, purpose: str) -> tuple[bool, str]:  # type: ignore[misc]
        return False, "unavailable"

    def verify_code(email: str, purpose: str, code: str) -> bool:  # type: ignore[misc]
        return False

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
def login(payload: LoginRequest, request: Request) -> TokenResponse:
    ratelimit.enforce(request, "login")
    user = db.get_user_by_email(payload.email)
    if not user or not user["actif"] or not verify_password(payload.password, user["hash_mdp"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    # A temporary password is only valid for 72h (server-side). After that the
    # member must contact the administration for a new one.
    if user.get("mdp_temporaire") and _temp_expired(user.get("mdp_expire_le")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="temporary password expired, contact the administration",
        )
    _record_session(str(user["id"]), user["role"], request)
    token = create_access_token(subject=str(user["id"]), role=user["role"])
    return TokenResponse(access_token=token, role=user["role"], doit_changer_mdp=bool(user.get("doit_changer_mdp")))


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _record_session(user_id: str, role: str, request: Request) -> None:
    """Log the connection (IP, device) for security tracking. Never breaks login."""
    try:
        ip = _client_ip(request)
        ua = (request.headers.get("user-agent") or "")[:300]
        db.execute(
            "INSERT INTO session (utilisateur_id, ip, appareil, cree_le) VALUES (%s, %s::inet, %s, now())",
            (user_id, ip, ua),
            role=role,
        )
        db.execute("UPDATE utilisateur SET dernier_login = now() WHERE id = %s", (user_id,), role=role)
    except Exception:  # noqa: BLE001 - tracking must never block authentication
        pass


def _temp_expired(expire_le: object) -> bool:
    if not isinstance(expire_le, datetime):
        return False
    now = datetime.now(tz=expire_le.tzinfo) if expire_le.tzinfo else datetime.utcnow()
    return now > expire_le


class PremiereConnexion(BaseModel):
    email: EmailStr
    mdp_temporaire: str
    nouveau_mdp: str
    code_otp: str


@router.post("/premiere-connexion", response_model=TokenResponse)
def premiere_connexion(payload: PremiereConnexion, request: Request) -> TokenResponse:
    """First login: validate the temporary password and an e-mail OTP, then set
    the member's own password (banking-style double validation)."""
    ratelimit.enforce(request, "premiere-connexion")
    if len(payload.nouveau_mdp) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short")
    user = db.get_user_by_email(payload.email)
    if not user or not user["actif"] or not verify_password(payload.mdp_temporaire, user["hash_mdp"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid temporary password")
    if user.get("mdp_temporaire") and _temp_expired(user.get("mdp_expire_le")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="temporary password expired")
    if not verify_code(str(payload.email), "login_2fa", payload.code_otp):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s, mdp_temporaire = false, doit_changer_mdp = false, "
        "mdp_expire_le = NULL WHERE id = %s",
        (hash_password(payload.nouveau_mdp), str(user["id"])),
    )
    token = create_access_token(subject=str(user["id"]), role=user["role"])
    return TokenResponse(access_token=token, role=user["role"], doit_changer_mdp=False)


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
def request_otp(payload: OtpRequest, request: Request) -> dict[str, object]:
    """Send a one-time code by e-mail for 2FA, password reset or signature.

    Always returns ok (does not reveal whether the e-mail exists), so it cannot
    be used to enumerate accounts.
    """
    ratelimit.enforce(request, "request-otp")
    if payload.purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown purpose")
    sent, provider = send_code(str(payload.email), payload.purpose)
    _otp_via_telegram(str(payload.email), payload.purpose)
    return {"ok": True, "sent": sent, "provider": provider}


def _otp_via_telegram(email: str, purpose: str) -> None:
    """Also deliver the code over Telegram when the member linked that channel."""
    try:
        from . import channels
        from .email_gateway import generate_code

        member = db.fetch_one(
            "SELECT m.telegram_chat_id, m.langue, m.prenoms FROM utilisateur u "
            "JOIN membre m ON m.id = u.membre_id WHERE u.email = %s",
            (email,),
        )
        if not member or not member.get("telegram_chat_id"):
            return
        code = generate_code(email, purpose)
        prenom = (str(member.get("prenoms") or "").split(" ")[0]) or ""
        en = (member.get("langue") == "en")
        if en:
            titre = "Your verification code"
            corps = f"Hello {prenom}, your ADSUM verification code is {code}. It expires in a few minutes; do not share it."  # noqa: E501
        else:
            titre = "Votre code de verification"
            corps = f"Bonjour {prenom}, votre code de verification ADSUM est {code}. Il expire dans quelques minutes ; ne le communiquez a personne."  # noqa: E501
        channels.send_telegram(str(member["telegram_chat_id"]), channels.Message(titre=titre, corps_text=corps))
    except Exception:  # noqa: BLE001 - OTP e-mail already sent; Telegram is best-effort
        pass


@router.post("/verify-otp")
def verify_otp(payload: OtpVerify) -> dict[str, object]:
    return {"valid": verify_code(str(payload.email), payload.purpose, payload.code)}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(payload: ResetRequest, request: Request) -> None:
    """Set a new password after a valid password_reset code, self-service."""
    ratelimit.enforce(request, "reset-password")
    if len(payload.nouveau) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short")
    if not verify_code(str(payload.email), "password_reset", payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    # Owner connection (role=None) bypasses RLS; scoped by e-mail. No reveal if absent.
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s WHERE email = %s",
        (hash_password(payload.nouveau), str(payload.email)),
    )
