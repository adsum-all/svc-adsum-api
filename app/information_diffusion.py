# ruff: noqa: E501
"""Professional multi-channel diffusion of an Information.

Kept out of ``information.py`` for size. Builds a professional, non-alarming
Telegram message (priority badge in text, bold title, short intro, bullet list,
official ADSUM link, institutional signature) and sends it to the eligible
recipients who linked Telegram. The in-app feed is always the source of truth;
Telegram and e-mail are relays that link back to ADSUM.
"""
from __future__ import annotations

import re
from typing import Any

from . import channels, db

# Bound the inline send so a very large audience never times out a serverless
# publish; anything beyond is left to the member app feed (the source of truth).
_MAX_TELEGRAM = 300

_BADGE = {"normale": "INFO", "importante": "IMPORTANT", "urgente": "URGENT"}


def _esc(s: str | None) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def portail_url() -> str:
    """Official member portal URL (admin-configurable), for the relay links."""
    return (channels.integration_value("portail_url") or "https://adsum-web-membre.pages.dev").rstrip("/")


def _contenu_telegram(contenu: str | None) -> str:
    """Plain, structured, escaped content for Telegram: markdown markers removed,
    "- " turned into bullets, blank lines collapsed. No raw HTML from the content."""
    s = contenu or ""
    s = s.replace("**", "").replace("__", "").replace("*", "")
    lignes = []
    for ligne in s.split("\n"):
        t = ligne.rstrip()
        if t.startswith("## "):
            lignes.append(f"<b>{_esc(t[3:])}</b>")
        elif t.startswith("- "):
            lignes.append(f"• {_esc(t[2:])}")
        elif t.startswith("> "):
            lignes.append(_esc(t[2:]))
        else:
            lignes.append(_esc(t))
    out = "\n".join(lignes)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def render_information_telegram(info: dict[str, Any], lien: str) -> str:
    """A professional, non-alarming Telegram message for one Information."""
    badge = _BADGE.get(str(info.get("priorite") or "normale"), "INFO")
    parts = [f"<b>{badge} - {_esc(info.get('titre'))}</b>"]
    if info.get("sous_titre"):
        parts.append(_esc(info.get("sous_titre")))
    corps = _contenu_telegram(info.get("contenu"))
    if corps:
        parts.append(corps)
    if info.get("signature"):
        parts.append(_esc(info.get("signature")))
    parts.append(f"\U0001f517 <a href=\"{_esc(lien)}\">Consulter dans ADSUM</a>")
    return "\n\n".join(parts)[:4000]


def diffuser_telegram(info_id: str, info: dict[str, Any], ids: list[str], role: str | None) -> dict[str, int]:
    """Send the professional Telegram message to recipients who linked Telegram.

    Best-effort, bounded and non-blocking: a send failure never fails the publish."""
    if not channels.telegram_configured() or not ids:
        return {"envoyes": 0, "eligibles": 0, "tronques": 0}
    lien = portail_url()
    html = render_information_telegram(info, lien)
    # Only members who linked Telegram; capped for a serverless-safe inline send.
    rows = db.fetch_all(
        "SELECT telegram_chat_id FROM membre WHERE id = ANY(%s) AND telegram_chat_id IS NOT NULL",
        (ids,), role=role,
    )
    eligibles = len(rows or [])
    a_envoyer = (rows or [])[:_MAX_TELEGRAM]
    msg = channels.Message(titre=str(info.get("titre") or "Information"), corps_text="", corps_html=html, type_notif="annonce")
    envoyes = 0
    for r in a_envoyer:
        try:
            if channels.send_telegram(str(r["telegram_chat_id"]), msg, contexte=f"information:{info_id}"):
                envoyes += 1
        except Exception:  # noqa: BLE001 - a broken relay never fails the publish
            continue
    return {"envoyes": envoyes, "eligibles": eligibles, "tronques": max(0, eligibles - _MAX_TELEGRAM)}
