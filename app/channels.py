"""Multi-channel notification delivery.

A single message is fanned out over the channels a member has opted into and that
are configured: always an in-app notification, plus e-mail, Telegram, WhatsApp or
SMS. Every send is a short, stateless HTTPS call (stdlib urllib), which fits the
serverless runtime; no channel ever raises into the caller.

Channel availability, as of this build:
- in-app: always on (notification table).
- e-mail: on when the e-mail gateway is configured (existing).
- Telegram: FREE. On as soon as ADSUM_TELEGRAM_BOT_TOKEN is set; the member links
  their account once (see the Telegram linking endpoints) to provide a chat id.
- WhatsApp: paid per message (Meta Cloud API). On only when the WABA token,
  phone-number id and an approved template are configured.
- SMS: placeholder, never attempted until a provider is chosen.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import db
from .config import settings

try:
    from .email_gateway import send_email
except Exception:  # keep notifications working even if the e-mail gateway fails to import

    def send_email(to: str, subject: str, text: str, html: str) -> tuple[bool, str]:  # type: ignore[misc]
        return False, "unavailable"


@dataclass
class Message:
    titre: str
    corps_text: str
    corps_html: str | None = None
    image_url: str | None = None
    type_notif: str = "systeme"
    # Approved WhatsApp template to use for this message, if any. None falls back
    # to the birthday template for the legacy birthday flow; an unknown or empty
    # template makes the WhatsApp send a no-op (the other channels still deliver).
    whatsapp_template: str | None = None


def _post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> tuple[bool, dict[str, object]]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return False, json.load(exc)
        except Exception:  # noqa: BLE001
            return False, {"error": "http_error"}
    except Exception:  # noqa: BLE001 - network failure must never break the caller
        return False, {"error": "network"}


# --- Integration config (admin-rotatable, falls back to environment) --------

def integration_value(cle: str) -> str:
    """Read a channel setting from the admin-managed table, else the environment."""
    try:
        row = db.fetch_one("SELECT valeur FROM integration_config WHERE cle = %s", (cle,))
        if row and row.get("valeur"):
            return str(row["valeur"])
    except Exception:  # noqa: BLE001 - fall back to env on any DB issue
        pass
    return getattr(settings, cle, "") or ""


def telegram_token() -> str:
    return integration_value("telegram_bot_token")


def telegram_username() -> str:
    return integration_value("telegram_bot_username")


def telegram_instruction_token() -> str:
    """Token of the DEDICATED instruction bot (distinct from the notification bot).
    Falls back to the notification bot token until a separate one is configured, so
    ingestion keeps working; configure telegram_instruction_bot_token to separate."""
    return integration_value("telegram_instruction_bot_token") or telegram_token()


def telegram_instruction_username() -> str:
    return integration_value("telegram_instruction_bot_username") or telegram_username()


def telegram_instruction_dedie() -> bool:
    """True when a separate instruction bot token is actually configured."""
    return bool(integration_value("telegram_instruction_bot_token"))


# --- Channel availability ---------------------------------------------------

def telegram_configured() -> bool:
    return bool(telegram_token())


def whatsapp_configured() -> bool:
    # The template is now chosen per message, so availability is just the account
    # credentials; a message without an approved template is skipped at send time.
    return bool(settings.whatsapp_token and settings.whatsapp_phone_number_id)


def sms_configured() -> bool:
    return bool(settings.sms_provider)


# --- Individual channel sends -----------------------------------------------

def send_telegram(chat_id: str, message: Message, contexte: str | None = None) -> bool:
    """Send a Telegram message. ``contexte`` tags the stored message id so a specific
    class of message (e.g. a login code, ``contexte='otp:login_2fa'``) can later be
    deleted precisely, without touching ordinary notifications."""
    token = telegram_token()
    if not token or not chat_id:
        return False
    url = f"{settings.telegram_api_base}/bot{token}/sendMessage"
    # Telegram rejects messages over 4096 characters; keep a safe margin.
    text = f"<b>{_esc(message.titre)}</b>\n{_esc(message.corps_text)}"[:4000]
    ok, body = _post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, {})
    if ok:
        _record_telegram_message(str(chat_id), body, contexte)
    return ok


def _record_telegram_message(chat_id: str, body: dict[str, object], contexte: str | None = None) -> None:
    """Store the outgoing message id so it can later be auto-deleted. Best-effort:
    a bookkeeping failure must never affect the delivery that already succeeded."""
    try:
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return
        db.execute(
            "INSERT INTO telegram_message (chat_id, message_id, contexte) VALUES (%s, %s, %s)",
            (chat_id, int(message_id), contexte),
            role=None,
        )
    except Exception:  # noqa: BLE001 - never break a successful send
        pass


def delete_telegram_message(chat_id: str, message_id: int) -> bool:
    """Delete one previously sent message. A bot may remove its own messages."""
    token = telegram_token()
    if not token:
        return False
    url = f"{settings.telegram_api_base}/bot{token}/deleteMessage"
    ok, _ = _post_json(url, {"chat_id": chat_id, "message_id": message_id}, {})
    return ok


def delete_tracked(chat_id: str, contexte: str, role: str | None = None) -> int:
    """Delete every still-live tracked message for a chat with the given context,
    then mark them purged. Used to remove a one-time code from the chat once it has
    been used (or superseded by a new one), so the secret does not linger. Returns
    how many Telegram confirmed deleted. Best-effort: never raises to the caller."""
    try:
        rows = db.fetch_all(
            "SELECT id, message_id FROM telegram_message "
            "WHERE chat_id = %s AND contexte = %s AND supprime_le IS NULL",
            (str(chat_id), contexte),
            role=role,
        )
    except Exception:  # noqa: BLE001 - bookkeeping table may be mid-migration
        return 0
    deleted = 0
    for r in rows or []:
        if delete_telegram_message(str(chat_id), int(r["message_id"])):
            deleted += 1
        try:
            db.execute("UPDATE telegram_message SET supprime_le = now() WHERE id = %s", (str(r["id"]),), role=role)
        except Exception:  # noqa: BLE001
            pass
    return deleted


def purge_old_telegram(role: str | None, retention_days: int | None = None, limit: int = 500) -> dict[str, int]:
    """Delete Telegram messages older than the retention window.

    The window is admin-configurable (integration_config ``telegram_retention_jours``),
    default 90 days. Already-deleted or vanished messages are marked done as well,
    so a message that Telegram no longer knows about is not retried forever.
    """
    days = retention_days if retention_days is not None else _retention_days()
    rows = db.fetch_all(
        "SELECT id, chat_id, message_id FROM telegram_message "
        "WHERE supprime_le IS NULL AND envoye_le < now() - make_interval(days => %s) "
        "ORDER BY envoye_le LIMIT %s",
        (days, limit),
        role=role,
    )
    deleted = 0
    for r in rows or []:
        ok = delete_telegram_message(str(r["chat_id"]), int(r["message_id"]))
        # Whether Telegram deleted it or it was already gone, stop tracking it.
        db.execute("UPDATE telegram_message SET supprime_le = now() WHERE id = %s", (str(r["id"]),), role=role)
        if ok:
            deleted += 1
    return {"examines": len(rows or []), "supprimes": deleted}


def _retention_days() -> int:
    raw = integration_value("telegram_retention_jours")
    try:
        value = int(raw) if raw else 90
    except (TypeError, ValueError):
        value = 90
    return max(1, value)


def send_whatsapp(numero: str, template_params: list[str], template_name: str | None = None) -> bool:
    """Send an approved template (the only proactive option on WhatsApp).

    ``template_name`` selects the approved template; it falls back to the birthday
    template for the legacy birthday flow. Without a resolved template the send is
    a no-op so a message type with no approved template never blocks the fan-out.
    """
    template = template_name or settings.whatsapp_template_anniversaire
    if not whatsapp_configured() or not numero or not template:
        return False
    url = f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": numero.lstrip("+"),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": settings.whatsapp_template_lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in template_params]}] if template_params else [],
        },
    }
    ok, _ = _post_json(url, payload, {"Authorization": f"Bearer {settings.whatsapp_token}"})
    return ok


def send_sms(numero: str, text: str) -> bool:
    """Send a transactional SMS through the configured provider (config-gated).

    Only 'brevo' is wired: it reuses the Brevo account key (email_api_key) and the
    transactional SMS API. Returns False (a clean no-op) when SMS is not configured,
    so the fan-out is never blocked by an unconfigured channel.
    """
    if not sms_configured() or not numero:
        return False
    if settings.sms_provider.lower() != "brevo" or not settings.email_api_key:
        return False
    payload = {
        "type": "transactional",
        "unicodeEnabled": True,
        "sender": (settings.sms_sender or "ADSUM")[:11],
        "recipient": numero.lstrip("+"),
        "content": text[:640],
    }
    ok, _ = _post_json(
        "https://api.brevo.com/v3/transactionalSMS/sms",
        payload,
        {"api-key": settings.email_api_key, "accept": "application/json"},
    )
    return ok


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Fan-out ----------------------------------------------------------------

def _record_echec(membre_id: str, role: str | None, type_cle: str, canal: str, detail: str) -> None:
    """Record a failed channel delivery for admin observability. Best-effort: it
    must never break the fan-out, and it stays silent if the ledger table is not
    present yet (migration not applied)."""
    try:
        db.execute(
            "INSERT INTO notification_echec (membre_id, type_cle, canal, detail) VALUES (%s, %s, %s, %s)",
            (membre_id, type_cle, canal, detail),
            role=role,
        )
    except Exception:  # noqa: BLE001 - observability must never affect delivery
        pass


def dispatch(membre_id: str, role: str | None, message: Message, whatsapp_params: list[str] | None = None, critique: bool = False, canaux_autorises: set[str] | None = None) -> list[str]:
    """Deliver a message to a member over every opted-in, configured channel.

    Always records an in-app notification. Returns the list of channels used.
    Never raises: a channel failure is skipped, not propagated.

    ``critique`` (security-sensitive messages: OTP, unusual login, security alert)
    bypasses both the admin channel kill-switch and the member's channel
    preferences: the message must reach the member on every channel it can.
    """
    used: list[str] = []
    # 1) In-app is unconditional.
    try:
        db.execute(
            "INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le) VALUES (%s, %s, %s, %s, false, now())",
            (membre_id, message.type_notif, message.titre, message.corps_text),
            role=role,
        )
        used.append("in-app")
    except Exception:  # noqa: BLE001
        pass

    prefs = db.fetch_one(
        "SELECT email, telegram, whatsapp, sms FROM preference_notification WHERE membre_id = %s",
        (membre_id,),
        role=role,
    ) or {"email": True, "telegram": True, "whatsapp": True, "sms": True}
    contact = db.fetch_one(
        "SELECT email, telegram_chat_id, whatsapp_numero, indicatif_telephone, telephone FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    ) or {}

    def autorise(canal: str) -> bool:
        # Critical messages ignore the admin kill-switch, the master channel switch
        # and the per-group matrix: security must reach the member on every channel.
        if critique:
            return True
        # Per-group matrix (when provided by notifier): the member turned this push
        # channel off for this notification group. In-app is unaffected (sent above).
        if canaux_autorises is not None and canal not in canaux_autorises:
            return False
        # Master channel switch + admin kill-switch.
        return canal_actif(canal) and bool(prefs.get(canal))

    # 2) E-mail (existing gateway).
    if autorise("email") and contact.get("email"):
        html = message.corps_html or f"<p>{message.corps_text}</p>"
        try:
            ok, info = send_email(str(contact["email"]), message.titre, message.corps_text, html)
            if ok:
                used.append("email")
            else:
                _record_echec(membre_id, role, message.type_notif, "email", str(info)[:250])
        except Exception as exc:  # noqa: BLE001
            _record_echec(membre_id, role, message.type_notif, "email", f"exception: {exc}"[:250])

    # 3) Telegram (free).
    if autorise("telegram") and contact.get("telegram_chat_id"):
        if send_telegram(str(contact["telegram_chat_id"]), message):
            used.append("telegram")
        else:
            _record_echec(membre_id, role, message.type_notif, "telegram", "envoi Telegram non abouti")

    # 4) WhatsApp (paid, config-gated). The message picks its approved template;
    # a type with no template is skipped inside send_whatsapp.
    if autorise("whatsapp") and contact.get("whatsapp_numero"):
        if send_whatsapp(str(contact["whatsapp_numero"]), whatsapp_params or [], message.whatsapp_template):
            used.append("whatsapp")
        else:
            _record_echec(membre_id, role, message.type_notif, "whatsapp", "envoi WhatsApp non abouti")

    # 5) SMS (config-gated, off until a provider is selected).
    numero_sms = _sms_numero(contact.get("indicatif_telephone"), contact.get("telephone"))
    if autorise("sms") and sms_configured() and numero_sms:
        if send_sms(numero_sms, f"{message.titre}: {message.corps_text}"):
            used.append("sms")
        else:
            _record_echec(membre_id, role, message.type_notif, "sms", "envoi SMS non abouti")

    return used


def _sms_numero(indicatif: object, telephone: object) -> str:
    """Build the international MSISDN (digits only) from the dialing code and the
    national number. Drops a single leading national trunk 0 when a code is present.
    Returns "" when the number is unusable."""
    ind = "".join(ch for ch in str(indicatif or "") if ch.isdigit())
    nat = "".join(ch for ch in str(telephone or "") if ch.isdigit())
    if not nat:
        return ""
    if ind and nat.startswith("0"):
        nat = nat[1:]
    numero = f"{ind}{nat}"
    return numero if len(numero) >= 8 else ""


def canal_actif(canal: str) -> bool:
    """Global admin On/Off for a delivery channel (default on). A kill-switch on
    top of the channel configuration and the per-member preferences."""
    return (integration_value(f"canal_{canal}") or "on").lower() != "off"
