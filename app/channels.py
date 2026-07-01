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


# --- Channel availability ---------------------------------------------------

def telegram_configured() -> bool:
    return bool(telegram_token())


def whatsapp_configured() -> bool:
    return bool(settings.whatsapp_token and settings.whatsapp_phone_number_id and settings.whatsapp_template_anniversaire)


def sms_configured() -> bool:
    return bool(settings.sms_provider)


# --- Individual channel sends -----------------------------------------------

def send_telegram(chat_id: str, message: Message) -> bool:
    token = telegram_token()
    if not token or not chat_id:
        return False
    url = f"{settings.telegram_api_base}/bot{token}/sendMessage"
    # Telegram rejects messages over 4096 characters; keep a safe margin.
    text = f"<b>{_esc(message.titre)}</b>\n{_esc(message.corps_text)}"[:4000]
    ok, body = _post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, {})
    if ok:
        _record_telegram_message(str(chat_id), body)
    return ok


def _record_telegram_message(chat_id: str, body: dict[str, object]) -> None:
    """Store the outgoing message id so it can later be auto-deleted. Best-effort:
    a bookkeeping failure must never affect the delivery that already succeeded."""
    try:
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None:
            return
        db.execute(
            "INSERT INTO telegram_message (chat_id, message_id) VALUES (%s, %s)",
            (chat_id, int(message_id)),
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


def send_whatsapp(numero: str, template_params: list[str]) -> bool:
    """Send an approved template (the only proactive option on WhatsApp)."""
    if not whatsapp_configured() or not numero:
        return False
    url = f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{settings.whatsapp_phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": numero.lstrip("+"),
        "type": "template",
        "template": {
            "name": settings.whatsapp_template_anniversaire,
            "language": {"code": settings.whatsapp_template_lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in template_params]}] if template_params else [],
        },
    }
    ok, _ = _post_json(url, payload, {"Authorization": f"Bearer {settings.whatsapp_token}"})
    return ok


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Fan-out ----------------------------------------------------------------

def dispatch(membre_id: str, role: str | None, message: Message, whatsapp_params: list[str] | None = None) -> list[str]:
    """Deliver a message to a member over every opted-in, configured channel.

    Always records an in-app notification. Returns the list of channels used.
    Never raises: a channel failure is skipped, not propagated.
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
        "SELECT email, telegram_chat_id, whatsapp_numero FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    ) or {}

    # 2) E-mail (existing gateway).
    if prefs.get("email") and contact.get("email"):
        html = message.corps_html or f"<p>{message.corps_text}</p>"
        try:
            ok, _ = send_email(str(contact["email"]), message.titre, message.corps_text, html)
            if ok:
                used.append("email")
        except Exception:  # noqa: BLE001
            pass

    # 3) Telegram (free).
    if prefs.get("telegram") and contact.get("telegram_chat_id") and send_telegram(str(contact["telegram_chat_id"]), message):
        used.append("telegram")

    # 4) WhatsApp (paid, config-gated).
    if prefs.get("whatsapp") and contact.get("whatsapp_numero") and send_whatsapp(str(contact["whatsapp_numero"]), whatsapp_params or []):
        used.append("whatsapp")

    return used
