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
    # Last resort when both the database and the environment are empty. It used to be
    # an ikmail address that config.py itself documents as rejected by Brevo, so the
    # one path meant to rescue a send guaranteed its failure. Now the validated
    # platform mailbox.
    email = _config_value("email_from", settings.email_from) or "adsum.sr@gmail.com"
    name = _config_value("email_from_name", settings.email_from_name) or "Sacerdoce Royal"
    return email, name


def _reply_to() -> str:
    """Where a member should write back, when it is not the sending mailbox.

    A notification mailbox is written to and never read. Without a reply address, a
    member answering a message writes into a void, which is worse than a no-reply
    that says so.
    """
    return _config_value("email_reply_to", settings.email_reply_to)


def _api_key(fournisseur: str = "") -> str:
    """The API key of one provider, falling back to the historical single key.

    One key used to serve every HTTP provider, so a chain of ``brevo,resend`` handed
    Brevo's key to Resend, which rejected it: the fallback failed at precisely the
    moment it existed for. Each provider now reads its own key, and the old shared
    key remains the fallback so nothing changes for a base configured before this.
    """
    if fournisseur:
        propre = _config_value(f"email_api_key_{fournisseur}", "")
        if propre:
            return propre
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
    digest = hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()
    number = int(digest, 16) % (10**CODE_DIGITS)
    return str(number).zfill(CODE_DIGITS)


def clean_code(code: str | None) -> str:
    """Keep only the digits of a submitted code.

    A pasted code frequently carries whitespace: the verification e-mail renders
    the code as one styled box per digit, so a copy often yields "1 2 3 4 5 6" (or
    with newlines). A plain ``strip()`` only trims the ends, leaving inner spaces
    that made a perfectly valid code look wrong ("code incorrect"). The code is
    always six digits, so reducing the input to its digits is safe and removes any
    space, tab, non-breaking space or stray separator introduced by copy-paste.
    Restricted to ASCII digits (``str.isdigit`` also accepts Unicode digits such as
    superscripts or other scripts, which have no place in a code)."""
    return "".join(ch for ch in (code or "") if "0" <= ch <= "9")


def verify_code(email: str, purpose: str, code: str) -> bool:
    """True when ``code`` matches the current or previous window.

    This is the raw HMAC check only. It is replay-tolerant on its own; callers
    that perform a state change (first login, password reset, e-signature) must
    use :func:`verify_and_consume` so the code becomes single-use.
    """
    cleaned = clean_code(code)
    return any(hmac.compare_digest(cleaned, generate_code(email, purpose, w)) for w in _windows())


def _code_fingerprint(email: str, purpose: str, code: str) -> str:
    """Keyed hash stored in the consumption ledger (never the code in clear).

    Uses the same digit-only normalisation as :func:`verify_code` so a code and its
    whitespace-carrying paste share one fingerprint (single-use stays consistent)."""
    msg = f"{email.lower().strip()}|{purpose}|{clean_code(code)}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


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
        # Most relays refuse to send as an address they do not own, and a residential
        # provider (Bouygues, Free) accepts only its own account address. Sending
        # "from" a Gmail address through such a relay is rejected or silently
        # rewritten, which is worse: the message leaves under an address the
        # organisation did not choose. This override lets the SMTP path carry its own
        # sender without disturbing the one the API providers use.
        propre = _config_value("email_smtp_from", "")
        if propre:
            sender_email = propre
        msg = EmailMessage()
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = to
        reply = _reply_to()
        if reply:
            msg["Reply-To"] = reply
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
        corps: dict[str, object] = {
            "sender": {"email": sender_email, "name": sender_name},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": text,
            "htmlContent": html,
        }
        reply = _reply_to()
        if reply:
            corps["replyTo"] = {"email": reply}
        _http_post("https://api.brevo.com/v3/smtp/email", {"api-key": _api_key("brevo")}, corps)


class ResendProvider:
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        sender_email, sender_name = _sender()
        corps: dict[str, object] = {
            "from": f"{sender_name} <{sender_email}>",
            "to": [to], "subject": subject, "text": text, "html": html,
        }
        reply = _reply_to()
        if reply:
            corps["reply_to"] = reply
        _http_post(
            "https://api.resend.com/emails",
            {"Authorization": f"Bearer {_api_key('resend')}"},
            corps,
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


def send_via(nom: str, to: str, subject: str, text: str, html: str) -> tuple[bool, str]:
    """Send through one named provider only, and return why it failed.

    Switching provider blind is how an organisation discovers, one week later and
    from a member, that nothing has left the building. This lets the back office
    prove a provider works BEFORE it is made the active one, and it deliberately
    does not fall back: a fallback here would report success for a provider that
    is in fact broken, which is the opposite of what a test is for.

    Returns ``(True, "")`` or ``(False, <provider error>)``.
    """
    cls = _PROVIDERS.get(nom)
    if cls is None:
        return False, f"fournisseur inconnu : {nom}"
    try:
        cls().send(to, subject, text, html)
    except Exception as exc:  # noqa: BLE001 - the reason is the payload of this call
        logger.warning("test send via %s to %s failed: %s", nom, to, exc)
        return False, str(exc)
    return True, ""


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
# What each code is for, without the organisation's name: the name is added at
# send time, because a constant evaluated at import would freeze whichever
# organisation happened to be configured when the process started.
PURPOSE_OBJETS = {
    "login_2fa": "votre code de connexion",
    "password_reset": "réinitialisation de mot de passe",
    "engagement": "code de signature des engagements",
}


def send_code(email: str, purpose: str) -> tuple[bool, str]:
    """Generate and send the one-time code for a purpose. Returns (sent, provider)."""
    from .email_templates import render_code_email
    from .marque import marque

    nom = marque().marque
    code = generate_code(email, purpose)
    subject = f"{nom}, {PURPOSE_OBJETS.get(purpose, 'votre code')}"
    intro = PURPOSE_INTRO.get(purpose, f"Voici votre code de vérification {nom}.")
    text = f"Votre code {nom} est : {code}\nIl est valable quelques minutes. Ne le partagez avec personne."
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
    from .marque import marque

    sent, _ = send_email(email, f"{marque().marque}, {titre}", text, html)
    return sent
