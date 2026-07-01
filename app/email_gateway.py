"""Provider-agnostic e-mail gateway with a stateless one-time code.

Design goals (business requirements):
- No lock-in: the active provider is chosen by configuration and can be changed
  by the business at any time (``ADSUM_EMAIL_PROVIDER``), without code changes.
- Free-friendly: SMTP (e.g. o2switch, Gmail), Brevo and Resend free tiers are
  supported out of the box; "console" logs the code for development.
- Stateless codes: one-time codes are derived by HMAC from the JWT secret, the
  e-mail, the purpose and a time window, so they need no table, no migration and
  work on serverless. A code is valid for the current and previous window.

The actual API key / SMTP password is supplied via environment variables; this
module never hardcodes a secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Protocol

from . import db
from .config import settings

logger = logging.getLogger("adsum.email")


def _config_value(cle: str, fallback: str) -> str:
    """Admin-managed value from integration_config, falling back to the environment."""
    try:
        row = db.fetch_one("SELECT valeur FROM integration_config WHERE cle = %s", (cle,))
        if row and row.get("valeur"):
            return str(row["valeur"])
    except Exception:  # noqa: BLE001 - never let config lookup break a send
        pass
    return fallback


def _sender() -> tuple[str, str]:
    """The active sender identity (admin-configurable, default Sacerdoce Royal)."""
    email = _config_value("email_from", settings.email_from) or "saintgabrielsacerdoceroyal@ikmail.com"
    name = _config_value("email_from_name", settings.email_from_name) or "Sacerdoce Royal"
    return email, name


def _api_key() -> str:
    return _config_value("email_api_key", settings.email_api_key)


def _http_post(url: str, headers: dict[str, str], body: dict[str, object]) -> int:
    """POST JSON using the standard library (httpx is unreliable on Vercel).

    Raises with the provider's error body on any non-2xx response so a rejected
    send is never mistaken for a success (no silent failure).
    """
    data = _json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ADSUM/1.0")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted provider URLs)
            if resp.status >= 300:
                raise RuntimeError(f"provider HTTP {resp.status}")
            return resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc

# One-time code parameters.
CODE_DIGITS = 6
WINDOW_SECONDS = 600  # 10 minutes


def _windows(now: int | None = None) -> list[int]:
    t = now if now is not None else int(time.time())
    current = t // WINDOW_SECONDS
    return [current, current - 1]


def generate_code(email: str, purpose: str, window: int | None = None) -> str:
    """Derive the 6-digit code for ``email``/``purpose`` in the given time window."""
    w = window if window is not None else _windows()[0]
    msg = f"{email.lower().strip()}|{purpose}|{w}".encode()
    digest = hmac.new(settings.jwt_secret.encode() or b"adsum", msg, hashlib.sha256).hexdigest()
    number = int(digest, 16) % (10**CODE_DIGITS)
    return str(number).zfill(CODE_DIGITS)


def verify_code(email: str, purpose: str, code: str) -> bool:
    """True when ``code`` matches the current or previous window.

    This is the raw HMAC check only. It is replay-tolerant on its own; callers
    that perform a state change (first login, password reset, e-signature) must
    use :func:`verify_and_consume` so the code becomes single-use.
    """
    cleaned = (code or "").strip()
    return any(hmac.compare_digest(cleaned, generate_code(email, purpose, w)) for w in _windows())


def _code_fingerprint(email: str, purpose: str, code: str) -> str:
    """Keyed hash stored in the consumption ledger (never the code in clear)."""
    msg = f"{email.lower().strip()}|{purpose}|{(code or '').strip()}".encode()
    return hmac.new(settings.jwt_secret.encode() or b"adsum", msg, hashlib.sha256).hexdigest()


def consume_code(email: str, purpose: str, code: str, ip: str | None = None) -> bool:
    """Atomically mark a code as used. True the first time, False on replay.

    The unique constraint on ``(email, purpose, code_hash)`` plus
    ``ON CONFLICT DO NOTHING`` makes this a single atomic step: two concurrent
    requests carrying the same code can never both receive ``True``.
    """
    fingerprint = _code_fingerprint(email, purpose, code)
    row = db.execute(
        "INSERT INTO code_consomme (email, purpose, code_hash, ip) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (email, purpose, code_hash) DO NOTHING RETURNING id",
        (email.lower().strip(), purpose, fingerprint, ip),
        role=None,
    )
    return row is not None


def verify_and_consume(email: str, purpose: str, code: str, ip: str | None = None) -> bool:
    """Verify a one-time code and burn it in the same call (true single-use).

    Returns ``True`` only when the code is cryptographically valid AND has never
    been consumed before for this ``(email, purpose)``. Any later attempt with
    the same code returns ``False``.
    """
    if not verify_code(email, purpose, code):
        return False
    return consume_code(email, purpose, code, ip)


class EmailProvider(Protocol):
    def send(self, to: str, subject: str, text: str, html: str) -> None: ...


class ConsoleProvider:
    """Development provider: records the message instead of sending it."""

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        print(f"[email:console] to={to} subject={subject!r} text_len={len(text)} html_len={len(html)}")


class SMTPProvider:
    """Send through a real mailbox's own SMTP (e.g. Infomaniak/ikmail).

    Sending from the mailbox provider's server means SPF/DKIM/DMARC align
    naturally, so mail reaches the inbox without owning a domain. Host, port and
    credentials are admin-configurable (integration_config), env as fallback.
    """

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        sender_email, sender_name = _sender()
        msg = EmailMessage()
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        host = _config_value("email_smtp_host", settings.email_smtp_host)
        port = int(_config_value("email_smtp_port", str(settings.email_smtp_port)) or "587")
        user = _config_value("email_smtp_user", settings.email_smtp_user)
        password = _config_value("email_smtp_password", settings.email_smtp_password)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(user, password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.starttls()
                srv.login(user, password)
                srv.send_message(msg)


class BrevoProvider:
    """Brevo transactional HTTP API (requires an xkeysib API key, not the SMTP key)."""

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        sender_email, sender_name = _sender()
        _http_post(
            "https://api.brevo.com/v3/smtp/email",
            {"api-key": _api_key()},
            {
                "sender": {"email": sender_email, "name": sender_name},
                "to": [{"email": to}],
                "subject": subject,
                "textContent": text,
                "htmlContent": html,
            },
        )


class ResendProvider:
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        sender_email, sender_name = _sender()
        _http_post(
            "https://api.resend.com/emails",
            {"Authorization": f"Bearer {_api_key()}"},
            {"from": f"{sender_name} <{sender_email}>", "to": [to], "subject": subject, "text": text, "html": html},
        )


_PROVIDERS: dict[str, type[EmailProvider]] = {
    "console": ConsoleProvider,
    "smtp": SMTPProvider,
    "brevo": BrevoProvider,
    "resend": ResendProvider,
}


def _provider_chain() -> list[str]:
    """The ordered list of providers to try (comma-separated, no lock-in)."""
    raw = (_config_value("email_provider", settings.email_provider) or "console").lower()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or ["console"]


def send_email(to: str, subject: str, text: str, html: str) -> tuple[bool, str]:
    """Send via the configured providers in order; never raise to the caller path.

    A rejected send is logged (with the provider error) rather than silently
    swallowed, so a failure is observable in the logs instead of looking like a
    success.
    """
    last = "console"
    errors: list[str] = []
    for name in _provider_chain():
        last = name
        cls = _PROVIDERS.get(name, ConsoleProvider)
        try:
            cls().send(to, subject, text, html)
            if errors:
                logger.warning("email to %s sent via %s after failures: %s", to, name, "; ".join(errors))
            return True, name
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            continue
    logger.error("email to %s FAILED on every provider: %s", to, "; ".join(errors))
    return False, last


PURPOSE_INTRO = {
    "login_2fa": "Saisissez ce code pour confirmer votre connexion à votre espace membre.",
    "password_reset": "Saisissez ce code pour réinitialiser votre mot de passe.",
    "engagement": "Saisissez ce code pour signer électroniquement vos engagements.",
}
PURPOSE_SUBJECTS = {
    "login_2fa": "ADSUM, votre code de connexion",
    "password_reset": "ADSUM, réinitialisation de mot de passe",
    "engagement": "ADSUM, code de signature des engagements",
}


def send_code(email: str, purpose: str) -> tuple[bool, str]:
    """Generate and send the one-time code for a purpose. Returns (sent, provider)."""
    from .email_templates import render_code_email

    code = generate_code(email, purpose)
    subject = PURPOSE_SUBJECTS.get(purpose, "ADSUM, votre code")
    intro = PURPOSE_INTRO.get(purpose, "Voici votre code de vérification ADSUM.")
    text = f"Votre code ADSUM est : {code}\nIl est valable quelques minutes. Ne le partagez avec personne."
    html = render_code_email(code, intro)
    sent, provider = send_email(email, subject, text, html)
    return sent, provider


def send_notification(
    email: str,
    titre: str,
    corps: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
) -> bool:
    """Send a professional notification e-mail (design-system styled)."""
    from .email_templates import render_notification_email

    html = render_notification_email(titre, corps, cta_label, cta_url)
    text = f"{titre}\n\n{corps}"
    sent, _ = send_email(email, f"ADSUM, {titre}", text, html)
    return sent
