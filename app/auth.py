"""Authentication endpoints: real login and current user."""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

from . import db, ratelimit
from .clientip import client_ip
from .config import settings
from .schemas import LoginRequest, LoginResponse, TokenResponse, UserMe
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


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request) -> LoginResponse:
    ratelimit.enforce(request, "login")
    # The member may identify by e-mail, ADSUM matricule or member code.
    user = db.get_user_by_identifier(payload.resolve_identifiant())
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
    # Second factor: if this member has enabled 2FA (or the universal baseline is
    # on) and the device is not trusted, do NOT issue a token. Send a one-time code
    # and ask the caller to confirm it at /login-verify. The password was already
    # validated, so this never leaks whether an account exists.
    if _mfa_should_challenge(user, request):
        canal = _send_login_code(str(user["email"]), user)
        return LoginResponse(otp_required=True, canal=canal)
    sid = _record_session(str(user["id"]), user["role"], request)
    token = create_access_token(subject=str(user["id"]), role=user["role"], sid=sid)
    return LoginResponse(
        access_token=token,
        role=user["role"],
        doit_changer_mdp=bool(user.get("doit_changer_mdp")),
        email=str(user["email"]),
    )


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


def _record_session(user_id: str, role: str, request: Request) -> str:
    """Create the revocable session row and return its id, so logout and admin
    revocation can kill this exact token.

    FAIL-CLOSED: if the session cannot be recorded, raise rather than issue a token
    without a ``sid``. A sid-less token skips the session-state check in ``current_user``
    and would stay valid until natural expiry despite a logout, an admin revocation or a
    password reset. The non-critical tracking side effects (last-login stamp, unusual
    device alert) are best-effort and never block issuance once the session row exists,
    so a hiccup on THEM can no longer downgrade the token to sid-less.

    The write is attempted twice. On a two-factor login the one-time code has already
    been burned by the time this runs, so a single transient failure on the connection
    pool costs the member their code and sends them back to ask for another one. One
    retry turns a hiccup into a delay of a few milliseconds instead.
    """
    ip = _client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:300]
    created = None
    nouvel_appareil = False
    derniere: Exception | None = None
    for tentative in range(2):
        try:
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
            if created:
                break
        except Exception as exc:  # noqa: BLE001 - retried once, then refused fail-closed
            derniere = exc
        if tentative == 0:
            time.sleep(0.15)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session non établie, réessayez dans un instant",
        ) from derniere
    sid = str(created["id"])
    try:
        db.execute("UPDATE utilisateur SET dernier_login = now() WHERE id = %s", (user_id,), role=role)
        if nouvel_appareil:
            _alerter_connexion_inhabituelle(user_id, role)
    except Exception:  # noqa: BLE001 - best-effort tracking, never invalidates a recorded session
        pass
    return sid


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


# --- Two-factor authentication ------------------------------------------------

def _device_hash(request: Request) -> str:
    """Stable-per-device identifier: a client-provided X-Device-Id (a random UUID
    the app stores locally) combined with the User-Agent, hashed so no raw value is
    stored. Falls back to the User-Agent alone when the header is absent."""
    device_id = (request.headers.get("x-device-id") or "").strip()
    ua = (request.headers.get("user-agent") or "").strip()
    raw = f"{device_id}|{ua}" if device_id else ua
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _device_label(request: Request) -> str:
    """Short human label for a trusted device, from the User-Agent. Best-effort."""
    ua = request.headers.get("user-agent") or ""
    for token, label in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Windows", "Windows"),
        ("Macintosh", "Mac"),
        ("Linux", "Linux"),
    ):
        if token in ua:
            return label
    return "Appareil"


def _trusted_device_valid(user_id: str, role: str, device_hash: str) -> bool:
    """True when this device is trusted and the 30-day window has not expired."""
    row = db.fetch_one(
        "SELECT 1 FROM appareil_confiance WHERE utilisateur_id = %s AND device_hash = %s "
        "AND revoque = false AND expire_le > now()",
        (user_id, device_hash),
        role=role,
    )
    return row is not None


def _is_staff(user: dict[str, object]) -> bool:
    """A staff account = any role beyond a plain member. Staff have access to apps
    beyond the member app, so 2FA is mandatory for them."""
    return str(user.get("role") or "") not in ("", "membre")


def _mfa_grace_depassee(user: dict[str, object]) -> bool:
    """True once a non-opted-in member's grace period (days since account creation)
    has elapsed, at which point 2FA becomes mandatory for them too. Fail closed on an
    anomalous creation date (missing/unknown type): treat it as grace over so a data
    gap enforces 2FA instead of silently disabling it."""
    cree = user.get("cree_le")
    if not isinstance(cree, datetime):
        return True
    if cree.tzinfo is None:
        cree = cree.replace(tzinfo=UTC)
    return (datetime.now(UTC) - cree).days >= settings.mfa_grace_jours


def _mfa_should_challenge(user: dict[str, object], request: Request) -> bool:
    """Decide whether the login must ask for a one-time code.

    Policy: 2FA is MANDATORY for staff (any role beyond member) and for members who
    opted in (mfa_actif). A plain member who has not opted in is NOT challenged during
    a grace period (no code is ever sent); after the grace it becomes mandatory. A
    trusted device then skips the code for its window (30 days)."""
    # Recette exemption: a throwaway demo account (@exemple.com) skips the code entirely
    # when demo login is explicitly enabled, so a role can be tested without a real inbox.
    # Inert by default and scoped to the demo domain: it never touches a real account.
    email = str(user.get("email") or "").lower()
    if settings.demo_login_enabled and email.endswith(settings.demo_login_domain):
        return False
    mfa_on = bool(user.get("mfa_actif"))
    staff = _is_staff(user) and settings.mfa_staff_obligatoire
    if mfa_on or staff or settings.mfa_baseline_enforced:
        engaged = True
    else:
        # Plain member (staff is handled above): governed only by the grace window,
        # regardless of whether the account is linked to a member row. The grace
        # helper is fail-closed on a missing creation date.
        engaged = _mfa_grace_depassee(user)
    if not engaged:
        return False
    device_hash = _device_hash(request)
    return not _trusted_device_valid(str(user["id"]), str(user["role"]), device_hash)


def _telegram_chat_id(user_id: str) -> str | None:
    """The member's linked Telegram chat id, or None. Never raises."""
    try:
        row = db.fetch_one(
            "SELECT m.telegram_chat_id FROM utilisateur u JOIN membre m ON m.id = u.membre_id "
            "WHERE u.id = %s",
            (user_id,),
        )
        return str(row["telegram_chat_id"]) if row and row.get("telegram_chat_id") else None
    except Exception:  # noqa: BLE001 - channel detection must never block login
        return None


def _send_login_code(email: str, user: dict[str, object], force_canal: str | None = None) -> str:
    """Send the login code on a SINGLE channel, Telegram first by default.

    The member asked to privilege Telegram: when they linked it, the code goes to
    Telegram ONLY (no duplicate e-mail every time). E-mail stays available on demand
    (the app offers "receive by e-mail", which calls this with ``force_canal='email'``)
    and is used automatically if the Telegram send fails, so no one is ever locked
    out. SMS is offered the same way once a provider is enabled. ``force_canal`` is
    the channel the member explicitly picked; without it the stored preference (or
    'auto') applies. Returns the channel actually used ('telegram' | 'email' | 'sms')."""
    canal = (force_canal or str(user.get("mfa_canal") or "auto")).lower()
    chat_id = _telegram_chat_id(str(user["id"]))
    if canal == "sms" and _otp_via_sms(email, user):
        return "sms"
    if canal in ("auto", "telegram") and chat_id and _otp_via_telegram(email, "login_2fa"):
        return "telegram"
    # E-mail: the member's choice, no Telegram link, or a failed Telegram/SMS send.
    send_code(email, "login_2fa")
    return "email"


def _otp_via_sms(email: str, user: dict[str, object]) -> bool:
    """Deliver the login code by SMS when a provider is configured. Returns whether it
    was actually sent (False also when SMS is off, so the caller falls back to e-mail)."""
    try:
        from . import channels
        from .email_gateway import generate_code

        if not channels.sms_configured():
            return False
        row = db.fetch_one(
            "SELECT m.indicatif_telephone, m.telephone FROM utilisateur u "
            "JOIN membre m ON m.id = u.membre_id WHERE u.id = %s",
            (str(user["id"]),),
        )
        numero = channels._sms_numero(row.get("indicatif_telephone"), row.get("telephone")) if row else ""
        if not numero:
            return False
        code = generate_code(email, "login_2fa")
        return channels.send_sms(numero, f"ADSUM, votre code de connexion : {code}. Il expire dans quelques minutes.")
    except Exception:  # noqa: BLE001 - SMS is best-effort; caller falls back to e-mail
        return False


def _trust_device(
    user_id: str, role: str, device_hash: str, label: str, strict: bool = False, technique: bool = False,
) -> None:
    """Remember this device so the member skips the code for the trust window. The
    unique (utilisateur_id, device_hash) key makes it an upsert that also refreshes
    the last verification time and expiry, and un-revokes a previously removed one.
    In reinforced mode (member opted in) the window is shorter, and a technical
    (applicative) super-admin is capped at the 72 h technical window."""
    if technique:
        days = int(settings.mfa_trust_days_technique)
    else:
        days = int(settings.mfa_trust_days_strict if strict else settings.mfa_trust_days)
    db.execute(
        "INSERT INTO appareil_confiance (utilisateur_id, device_hash, libelle, "
        "derniere_verif_le, expire_le, revoque) "
        "VALUES (%s, %s, %s, now(), now() + make_interval(days => %s), false) "
        "ON CONFLICT (utilisateur_id, device_hash) DO UPDATE SET "
        "derniere_verif_le = now(), expire_le = now() + make_interval(days => %s), "
        "revoque = false, libelle = EXCLUDED.libelle",
        (user_id, device_hash, label, days, days),
        role=role,
    )


class LoginVerify(BaseModel):
    # Same identifier options as /login (e-mail, matricule or member code). Bounds
    # mirror LoginRequest so an oversized identifier/password can never reach the
    # resolver or the Argon2 verification (CPU/memory amplification guard).
    email: str | None = Field(default=None, max_length=200)
    identifiant: str | None = Field(default=None, max_length=200)
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=12)
    # When true, this device is trusted for the configured window (default 30 days).
    faire_confiance: bool = False

    def resolve_identifiant(self) -> str:
        return (self.identifiant or self.email or "").strip()


@router.post("/login-verify", response_model=LoginResponse)
def login_verify(payload: LoginVerify, request: Request) -> LoginResponse:
    """Second step of a 2FA login: re-check the password, consume the one-time code
    and only then issue the session token. Optionally trust this device."""
    ratelimit.enforce(request, "login-verify")
    user = db.get_user_by_identifier(payload.resolve_identifiant())
    password_ok = verify_password_or_dummy(payload.password, user["hash_mdp"] if user else None)
    if not user or not user["actif"] or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if user.get("mdp_temporaire") and _temp_expired(user.get("mdp_expire_le")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="temporary password expired")
    # The one-time code is always keyed on the member's canonical e-mail, whichever
    # identifier they signed in with, so matricule/member-code logins verify correctly.
    email_otp = str(user["email"])
    ratelimit.otp_guard(email_otp, "login_2fa")
    if not verify_and_consume(email_otp, "login_2fa", payload.code, ip=client_ip(request)):
        ratelimit.otp_failure(email_otp, "login_2fa")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid code")
    # The code has been used: remove it from the member's Telegram chat.
    _delete_otp_telegram(email_otp, "login_2fa")
    if payload.faire_confiance:
        # Shorter trust window (re-verify sooner) for anyone the policy holds to
        # mandatory 2FA: members who opted in AND all staff. A trusted staff device
        # must not skip the code for the full 30 days.
        _trust_device(
            str(user["id"]), str(user["role"]), _device_hash(request), _device_label(request),
            strict=bool(user.get("mfa_actif")) or _is_staff(user),
            technique=bool(user.get("acces_technique_global")),
        )
    sid = _record_session(str(user["id"]), user["role"], request)
    token = create_access_token(subject=str(user["id"]), role=user["role"], sid=sid)
    from . import audit

    audit.log(str(user["id"]), user["role"], "connexion_2fa", "utilisateur", str(user["id"]),
              {"appareil_confiance": bool(payload.faire_confiance)})
    return LoginResponse(
        access_token=token,
        role=user["role"],
        doit_changer_mdp=bool(user.get("doit_changer_mdp")),
        email=email_otp,
    )


class LoginOtpResend(BaseModel):
    # Same identifier options as /login (e-mail, matricule or member code). Bounded
    # like LoginRequest so an oversized identifier never reaches the resolver.
    email: str | None = Field(default=None, max_length=200)
    identifiant: str | None = Field(default=None, max_length=200)
    # Channel the member picked to receive the code: email | telegram | sms | auto.
    canal: str = Field(default="email", max_length=16)

    def resolve_identifiant(self) -> str:
        return (self.identifiant or self.email or "").strip()


@router.post("/login-otp")
def login_otp(payload: LoginOtpResend, request: Request) -> dict[str, object]:
    """Re-send the login code on a channel the member chooses.

    Telegram is the default at login; this lets the member switch to e-mail (or SMS
    once a provider is enabled), or simply resend. It never sends a code to a member
    still in the 2FA grace period (they must not receive codes), and it always returns
    ok so it cannot be used to tell whether an account exists."""
    ratelimit.enforce(request, "request-otp")
    canal = payload.canal.lower()
    if canal not in ("email", "telegram", "sms", "auto"):
        canal = "email"
    # The response NEVER depends on the account (existence, activity, engagement,
    # Telegram linkage): it always echoes the requested channel. This closes an
    # account-enumeration oracle. Confirming an e-mail belongs to a member would
    # leak a religious affiliation (RGPD art. 9), so the endpoint must be silent
    # about account state. The real delivery happens internally, with fallback.
    reponse_canal = "email" if canal == "auto" else canal
    user = db.get_user_by_identifier(payload.resolve_identifiant())
    if user and user.get("actif"):
        engaged = (
            bool(user.get("mfa_actif"))
            or (_is_staff(user) and settings.mfa_staff_obligatoire)
            or bool(settings.mfa_baseline_enforced)
            or _mfa_grace_depassee(user)
        )
        # A member in the grace window never receives a code. A per-target throttle
        # stops the endpoint being looped to bomb a member with codes; it is silent
        # (never revealed in the response) and fails open on a DB hiccup. The code is
        # always sent to the member's canonical e-mail/Telegram, not the raw identifier.
        email_cible = str(user["email"]).strip().lower()
        if engaged and ratelimit.throttle_action(f"otp-send:{email_cible}", 4, 300):
            _send_login_code(email_cible, user, force_canal=canal)
    return {"ok": True, "canal": reponse_canal}


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
        acces_technique_global=bool(user.get("acces_technique_global")),
        niveau_technique=(str(user["niveau_technique"]) if user.get("niveau_technique") else None),
    )


@router.get("/me", response_model=UserMe)
def me(user: Annotated[UserMe, Depends(current_user)]) -> UserMe:
    return user


class PreferencesCompte(BaseModel):
    """Personal, non-sensitive display preferences of the signed-in account.

    Strictly typed keys only: an arbitrary blob is never accepted, so the column can
    never be abused to store sensitive or unbounded data."""

    vue_evenements: str | None = Field(default=None, pattern="^(calendrier|liste)$")
    theme: str | None = Field(default=None, pattern="^(light|dark|system)$")


@router.get("/me/preferences", response_model=PreferencesCompte)
def mes_preferences(user: Annotated[UserMe, Depends(current_user)]) -> PreferencesCompte:
    """The account's own display preferences (server-side, so they follow the account
    across browsers and devices instead of living in one browser's storage)."""
    row = db.fetch_one("SELECT preferences FROM utilisateur WHERE id = %s", (user.id,), role=user.role)
    prefs = (row or {}).get("preferences") or {}
    if not isinstance(prefs, dict):
        prefs = {}
    vue = prefs.get("vue_evenements")
    theme = prefs.get("theme")
    return PreferencesCompte(
        vue_evenements=vue if vue in ("calendrier", "liste") else None,
        theme=theme if theme in ("light", "dark", "system") else None,
    )


@router.put("/me/preferences", response_model=PreferencesCompte)
def set_mes_preferences(
    payload: PreferencesCompte, user: Annotated[UserMe, Depends(current_user)]
) -> PreferencesCompte:
    """Merge the provided known keys into the account's preferences (jsonb merge, so
    unrelated keys survive). Only the caller's own account is ever touched."""
    import json as _json

    fournis = payload.model_dump(exclude_unset=True, exclude_none=True)
    if fournis:
        db.execute(
            "UPDATE utilisateur SET preferences = coalesce(preferences, '{}'::jsonb) || %s::jsonb WHERE id = %s",
            (_json.dumps(fournis), user.id),
            role=user.role,
        )
        from . import audit

        audit.log(
            user.id, user.role, "compte_preferences_maj", "utilisateur", user.id,
            {"champs": sorted(fournis.keys())},
        )
    return mes_preferences(user)


class MfaStatut(BaseModel):
    mfa_actif: bool
    est_staff: bool
    obligatoire: bool  # 2FA required for this account (staff, opted in, or grace over)
    recommande: bool   # show the "activate 2FA" recommendation (not yet mandatory)
    jours_avant_obligation: int | None = None  # days left before it becomes mandatory


@router.get("/mfa-statut", response_model=MfaStatut)
def mfa_statut(user: Annotated[UserMe, Depends(current_user)]) -> MfaStatut:
    """The member's 2FA situation, so the app can show the security recommendation
    and, for a plain member in the grace period, the countdown before 2FA becomes
    mandatory. Staff are always mandatory."""
    row = db.get_user_by_email(user.email) or {}
    mfa_on = bool(row.get("mfa_actif"))
    staff = _is_staff(row) and settings.mfa_staff_obligatoire
    obligatoire = mfa_on or staff or settings.mfa_baseline_enforced or _mfa_grace_depassee(row)
    jours = None
    cree = row.get("cree_le")
    if not mfa_on and not staff and isinstance(cree, datetime):
        c = cree if cree.tzinfo else cree.replace(tzinfo=UTC)
        jours = max(0, settings.mfa_grace_jours - (datetime.now(UTC) - c).days)
    return MfaStatut(
        mfa_actif=mfa_on, est_staff=_is_staff(row), obligatoire=bool(obligatoire),
        recommande=not mfa_on, jours_avant_obligation=jours,
    )


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


def _otp_via_telegram(email: str, purpose: str) -> bool:
    """Deliver the code over Telegram when the member linked that channel.

    Any previous still-live code for this chat/purpose is removed first, so only the
    newest code remains in the chat. The message is tagged with its context so it can
    be deleted again once the code has been used. Returns whether it was sent."""
    try:
        from . import channels
        from .email_gateway import generate_code

        member = db.fetch_one(
            "SELECT m.telegram_chat_id, m.langue, m.prenoms FROM utilisateur u "
            "JOIN membre m ON m.id = u.membre_id WHERE u.email = %s",
            (email,),
        )
        if not member or not member.get("telegram_chat_id"):
            return False
        chat_id = str(member["telegram_chat_id"])
        contexte = f"otp:{purpose}"
        channels.delete_tracked(chat_id, contexte)
        code = generate_code(email, purpose)
        prenom = (str(member.get("prenoms") or "").split(" ")[0]) or ""
        en = (member.get("langue") == "en")
        if en:
            titre = "Your verification code"
            corps = f"Hello {prenom}, your ADSUM verification code is {code}. It expires in a few minutes; do not share it."  # noqa: E501
        else:
            titre = "Votre code de vérification"
            corps = f"Bonjour {prenom}, votre code de vérification ADSUM est {code}. Il expire dans quelques minutes ; ne le communiquez à personne."  # noqa: E501
        return channels.send_telegram(chat_id, channels.Message(titre=titre, corps_text=corps), contexte=contexte)
    except Exception:  # noqa: BLE001 - Telegram is best-effort; caller may fall back
        return False


def _delete_otp_telegram(email: str, purpose: str) -> None:
    """Remove the one-time code from the member's Telegram chat once it has been used,
    so the secret does not linger. Best-effort, never raises."""
    try:
        from . import channels

        member = db.fetch_one(
            "SELECT m.telegram_chat_id FROM utilisateur u JOIN membre m ON m.id = u.membre_id "
            "WHERE u.email = %s",
            (email,),
        )
        if member and member.get("telegram_chat_id"):
            channels.delete_tracked(str(member["telegram_chat_id"]), f"otp:{purpose}")
    except Exception:  # noqa: BLE001
        pass


@router.post("/verify-otp")
def verify_otp(payload: OtpVerify, request: Request) -> dict[str, object]:
    # Rate-limited per (trusted) IP and locked out per account after repeated
    # failures, so this verification cannot be used as a brute-force oracle.
    if payload.purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown purpose")
    ratelimit.enforce(request, "verify-otp")
    # This endpoint is unauthenticated (no password, no session), so its failures
    # are counted in a SEPARATE key namespace ("check-<purpose>"). This prevents an
    # attacker who only knows an e-mail from poisoning the lockout that gates the
    # password-protected flows (/login-verify, /premiere-connexion, /reset-password),
    # which would otherwise let them lock a member out of their own account.
    guard_purpose = f"check-{payload.purpose}"
    ratelimit.otp_guard(str(payload.email), guard_purpose)
    valid = verify_code(str(payload.email), payload.purpose, payload.code)
    if not valid:
        ratelimit.otp_failure(str(payload.email), guard_purpose)
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
    _delete_otp_telegram(str(payload.email), "password_reset")
    # Owner connection (role=None) bypasses RLS; scoped by e-mail. No reveal if absent.
    # Also clear the temporary-password flags (a reset produces a definitive
    # password), and revoke every existing session so a stolen token cannot survive
    # the recovery. The password value is never logged.
    # Resolve the account case-insensitively, exactly like login (db.get_user_by_email
    # uses lower(email) against the unique lower(email) index). E-mails are stored in
    # their original case, so a case-sensitive match here would silently update zero
    # rows for any account whose stored e-mail has an uppercase letter, returning 204
    # while the password never changed and the sessions were never revoked.
    updated = db.execute(
        "UPDATE utilisateur SET hash_mdp = %s, mdp_temporaire = false, mdp_expire_le = NULL, "
        "doit_changer_mdp = false WHERE lower(email) = lower(%s) RETURNING id",
        (hash_password(payload.nouveau), str(payload.email)),
    )
    if updated:
        uid = str(updated["id"])
        db.execute("UPDATE session SET fin = now(), revoque = true WHERE utilisateur_id = %s AND fin IS NULL", (uid,))
        from . import audit

        audit.log(uid, "membre", "reinitialisation_mdp", "utilisateur", uid, {"sessions_revoquees": True})
