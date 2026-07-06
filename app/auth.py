"""Authentication endpoints: real login and current user."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from . import db, ratelimit
from .clientip import client_ip
from .schemas import LoginRequest, TokenResponse, UserMe
from .security import create_access_token, decode_access_token, hash_password, verify_password_or_dummy

try:
    from .email_gateway import send_code, verify_and_consume, verify_code
except Exception:  # keep the API up even if the e-mail gateway fails to import

    def send_code(email: str, purpose: str) -> tuple[bool, str]:  # type: ignore[misc]
        return False, "unavailable"

    def verify_code(email: str, purpose: str, code: str) -> bool:  # type: ignore[misc]
        return False

    def verify_and_consume(email: str, purpose: str, code: str, ip: str | None = None) -> bool:  # type: ignore[misc]
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
    password_ok = verify_password_or_dummy(payload.password, user["hash_mdp"] if user else None)
    if not user or not user["actif"] or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    # A temporary password is only valid for 72h (server-side). After that the
    # member must contact the administration for a new one.
    if user.get("mdp_temporaire") and _temp_expired(user.get("mdp_expire_le")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="temporary password expired, contact the administration",
        )
    sid = _record_session(str(user["id"]), user["role"], request)
    token = create_access_token(subject=str(user["id"]), role=user["role"], sid=sid)
    return TokenResponse(access_token=token, role=user["role"], doit_changer_mdp=bool(user.get("doit_changer_mdp")))


def _geo(request: Request) -> tuple[str | None, str | None, str | None]:
    """Coarse audit geolocation from the edge headers (country/city/region). No
    external call, no misleading precision; missing headers simply stay null."""
    h = request.headers
    pays = h.get("x-vercel-ip-country") or h.get("cf-ipcountry")
    ville = h.get("x-vercel-ip-city")
    region = h.get("x-vercel-ip-country-region") or h.get("x-vercel-ip-region")
    from urllib.parse import unquote
    return (pays or None, unquote(ville) if ville else None, region or None)


def _client_ip(request: Request) -> str | None:
    return client_ip(request)


def _record_session(user_id: str, role: str, request: Request) -> str | None:
    """Log the connection (IP, device, coarse geolocation) for security tracking
    and return the new session id so logout can close it. Never breaks login."""
    try:
        ip = _client_ip(request)
        ua = (request.headers.get("user-agent") or "")[:300]
        pays, ville, region = _geo(request)
        # Detect a login from a device we have never seen for this account, but
        # only once the account already has a history (never on the first login).
        seen = db.fetch_one(
            "SELECT count(*) AS total, count(*) FILTER (WHERE appareil = %s) AS meme "
            "FROM session WHERE utilisateur_id = %s",
            (ua, user_id),
            role=role,
        ) or {"total": 0, "meme": 0}
        nouvel_appareil = int(seen.get("total") or 0) > 0 and int(seen.get("meme") or 0) == 0
        created = db.execute(
            "INSERT INTO session (utilisateur_id, ip, appareil, pays, ville, region, cree_le) "
            "VALUES (%s, %s::inet, %s, %s, %s, %s, now()) RETURNING id",
            (user_id, ip, ua, pays, ville, region),
            role=role,
        )
        db.execute("UPDATE utilisateur SET dernier_login = now() WHERE id = %s", (user_id,), role=role)
        if nouvel_appareil:
            _alerter_connexion_inhabituelle(user_id, role)
        return str(created["id"]) if created else None
    except Exception:  # noqa: BLE001 - tracking must never block authentication
        return None


def _alerter_connexion_inhabituelle(user_id: str, role: str) -> None:
    """Warn the member (critical channel) that a new device signed in. Best-effort:
    a notification failure must never affect the login that already succeeded."""
    try:
        from .notifications import notifier

        row = db.fetch_one(
            "SELECT m.id AS membre_id, m.prenoms FROM utilisateur u "
            "JOIN membre m ON m.id = u.membre_id WHERE u.id = %s",
            (user_id,),
            role=role,
        )
        if not row or not row.get("membre_id"):
            return
        prenom = (str(row.get("prenoms") or "").split(" ")[0]) or "cher membre"
        # Anti-flood: this alert is critical, so it bypasses the member's channel
        # preferences and the admin kill-switch. A holder of valid credentials could
        # otherwise loop logins with a varying User-Agent and drown the member. Cap
        # it to at most one alert per member and per hour via the dedup log.
        bucket = datetime.now(tz=UTC).strftime("%Y-%m-%d-%H")
        notifier(str(row["membre_id"]), role, "connexion_inhabituelle", {"prenom": prenom}, ref_id=bucket, dedup=True)
    except Exception:  # noqa: BLE001 - a security notice must never break authentication
        pass


def _temp_expired(expire_le: object) -> bool:
    # Fail closed: a temporary password without a valid expiry (NULL/unknown type)
    # is treated as expired, never as valid forever.
    if not isinstance(expire_le, datetime):
        return True
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
    password_ok = verify_password_or_dummy(payload.mdp_temporaire, user["hash_mdp"] if user else None)
    if not user or not user["actif"] or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid temporary password")
    if user.get("mdp_temporaire") and _temp_expired(user.get("mdp_expire_le")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="temporary password expired")
    # Per-account OTP lockout, independent of the caller IP, so the login_2fa code
    # cannot be brute-forced by rotating IPs. This endpoint sets the definitive
    # password, so a forced OTP would mean a full account takeover.
    ratelimit.otp_guard(str(payload.email), "login_2fa")
    consent_ip = client_ip(request)
    if not verify_and_consume(str(payload.email), "login_2fa", payload.code_otp, ip=consent_ip):
        ratelimit.otp_failure(str(payload.email), "login_2fa")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s, mdp_temporaire = false, doit_changer_mdp = false, "
        "mdp_expire_le = NULL WHERE id = %s",
        (hash_password(payload.nouveau_mdp), str(user["id"])),
    )
    # Emit a session-bound token like login, so logout and admin revocation can
    # invalidate this first-login token before its natural 14-day expiry.
    sid = _record_session(str(user["id"]), user["role"], request)
    token = create_access_token(subject=str(user["id"]), role=user["role"], sid=sid)
    from . import audit

    audit.log(str(user["id"]), user["role"], "premiere_connexion", "utilisateur", str(user["id"]), {"activation": True})
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
    # A revoked or closed session invalidates the token immediately, so logout and
    # admin revocation take effect before the token's natural expiry.
    sid = claims.get("sid")
    if sid:
        session = db.fetch_one(
            "SELECT 1 FROM session WHERE id = %s AND revoque = false AND fin IS NULL",
            (str(sid),),
            role=claims["role"],
        )
        if not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session closed")
    return UserMe(
        id=str(user["id"]),
        email=user["email"],
        role=user["role"],
        membre_id=str(user["membre_id"]) if user["membre_id"] else None,
        session_id=str(claims["sid"]) if claims.get("sid") else None,
    )


@router.get("/me", response_model=UserMe)
def me(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
    return user


@router.post("/logout")
def logout(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Close the current session: mark its end and revoke it, so the security log
    holds a real connection/disconnection with a computable duration."""
    if user.session_id:
        # Imported here, not at module level: auth is a foundational module and an
        # eager import of audit (which depends on permissions_rbac -> auth) would
        # close an import cycle. Audit is only needed at logout, at request time.
        from . import audit

        db.execute(
            "UPDATE session SET fin = now(), revoque = true WHERE id = %s AND fin IS NULL",
            (user.session_id,),
            role=user.role,
        )
        audit.log(user.id, user.role, "deconnexion", "session", user.session_id, {})
    return {"ok": True}


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
            titre = "Votre code de vérification"
            corps = f"Bonjour {prenom}, votre code de vérification ADSUM est {code}. Il expire dans quelques minutes ; ne le communiquez à personne."  # noqa: E501
        channels.send_telegram(str(member["telegram_chat_id"]), channels.Message(titre=titre, corps_text=corps))
    except Exception:  # noqa: BLE001 - OTP e-mail already sent; Telegram is best-effort
        pass


@router.post("/verify-otp")
def verify_otp(payload: OtpVerify, request: Request) -> dict[str, object]:
    # Rate-limited per (trusted) IP and locked out per account after repeated
    # failures, so this verification cannot be used as a brute-force oracle.
    ratelimit.enforce(request, "verify-otp")
    ratelimit.otp_guard(str(payload.email), payload.purpose)
    valid = verify_code(str(payload.email), payload.purpose, payload.code)
    if not valid:
        ratelimit.otp_failure(str(payload.email), payload.purpose)
    return {"valid": valid}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(payload: ResetRequest, request: Request) -> None:
    """Set a new password after a valid password_reset code, self-service."""
    ratelimit.enforce(request, "reset-password")
    if len(payload.nouveau) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short")
    ratelimit.otp_guard(str(payload.email), "password_reset")
    if not verify_and_consume(str(payload.email), "password_reset", payload.code, ip=client_ip(request)):
        ratelimit.otp_failure(str(payload.email), "password_reset")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    # Owner connection (role=None) bypasses RLS; scoped by e-mail. No reveal if absent.
    # Also clear the temporary-password flags (a reset produces a definitive
    # password), and revoke every existing session so a stolen token cannot survive
    # the recovery. The password value is never logged.
    updated = db.execute(
        "UPDATE utilisateur SET hash_mdp = %s, mdp_temporaire = false, mdp_expire_le = NULL, "
        "doit_changer_mdp = false WHERE email = %s RETURNING id",
        (hash_password(payload.nouveau), str(payload.email)),
    )
    if updated:
        uid = str(updated["id"])
        db.execute("UPDATE session SET fin = now(), revoque = true WHERE utilisateur_id = %s AND fin IS NULL", (uid,))
        from . import audit

        audit.log(uid, "membre", "reinitialisation_mdp", "utilisateur", uid, {"sessions_revoquees": True})
