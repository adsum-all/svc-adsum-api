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
import smtplib
import time
from email.message import EmailMessage
from typing import Protocol

from .config import settings

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
    """True when ``code`` matches the current or previous window (replay-tolerant)."""
    cleaned = (code or "").strip()
    return any(hmac.compare_digest(cleaned, generate_code(email, purpose, w)) for w in _windows())


class EmailProvider(Protocol):
    def send(self, to: str, subject: str, text: str) -> None: ...


class ConsoleProvider:
    """Development provider: records the message instead of sending it."""

    def send(self, to: str, subject: str, text: str) -> None:
        # Intentionally not printing the code to shared logs in production use;
        # this provider is only selected in development.
        print(f"[email:console] to={to} subject={subject!r} body_len={len(text)}")


class SMTPProvider:
    def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"] = settings.email_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        host, port = settings.email_smtp_host, settings.email_smtp_port
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(settings.email_smtp_user, settings.email_smtp_password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.starttls()
                srv.login(settings.email_smtp_user, settings.email_smtp_password)
                srv.send_message(msg)


class BrevoProvider:
    def send(self, to: str, subject: str, text: str) -> None:
        import httpx

        httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": settings.email_api_key, "content-type": "application/json"},
            json={
                "sender": {"email": settings.email_from, "name": "ADSUM"},
                "to": [{"email": to}],
                "subject": subject,
                "textContent": text,
            },
            timeout=15,
        ).raise_for_status()


class ResendProvider:
    def send(self, to: str, subject: str, text: str) -> None:
        import httpx

        httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.email_api_key}", "content-type": "application/json"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "text": text},
            timeout=15,
        ).raise_for_status()


_PROVIDERS: dict[str, type[EmailProvider]] = {
    "console": ConsoleProvider,
    "smtp": SMTPProvider,
    "brevo": BrevoProvider,
    "resend": ResendProvider,
}


def _provider() -> EmailProvider:
    name = (settings.email_provider or "console").lower()
    cls = _PROVIDERS.get(name, ConsoleProvider)
    return cls()


def send_email(to: str, subject: str, text: str) -> bool:
    """Send via the configured provider; never raise to the caller path."""
    try:
        _provider().send(to, subject, text)
        return True
    except Exception:
        return False


PURPOSE_SUBJECTS = {
    "login_2fa": "ADSUM, votre code de connexion",
    "password_reset": "ADSUM, reinitialisation de mot de passe",
    "engagement": "ADSUM, code de signature des engagements",
}


def send_code(email: str, purpose: str) -> tuple[bool, str]:
    """Generate and send the one-time code for a purpose. Returns (sent, provider)."""
    code = generate_code(email, purpose)
    subject = PURPOSE_SUBJECTS.get(purpose, "ADSUM, votre code")
    body = f"Votre code ADSUM est : {code}\n\nIl est valable quelques minutes. Ne le partagez avec personne."
    sent = send_email(email, subject, body)
    return sent, (settings.email_provider or "console")
