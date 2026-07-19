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


def _ligne_texte(t: str) -> str:
    if t.startswith("- "):
        return f"- {t[2:]}"
    if t.startswith("## "):
        return t[3:]
    if t.startswith("> "):
        return t[2:]
    return t


def _contenu_texte(contenu: str | None) -> str:
    """Plain, structured text for e-mail (markers removed, bullets kept)."""
    s = (contenu or "").replace("**", "").replace("__", "").replace("*", "")
    out = [_ligne_texte(ligne.rstrip()) for ligne in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def diffuser_email(info: dict[str, Any], ids: list[str], role: str | None) -> dict[str, int]:
    """Send a responsive institutional e-mail to recipients who have an address.
    Best-effort and bounded; a failure never fails the publish."""
    from . import email_gateway, email_templates
    lien = portail_url()
    badge = _BADGE.get(str(info.get("priorite") or "normale"), "INFO")
    titre = f"{badge} - {info.get('titre') or 'Information'}"
    corps_parts = [p for p in [str(info.get("sous_titre") or ""), _contenu_texte(info.get("contenu"))] if p]
    corps = "\n\n".join(corps_parts) or (info.get("titre") or "")
    signature = channels.integration_value("org_signature") or channels.integration_value("signature") or None
    site = channels.integration_value("org_site") or None
    html = email_templates.render_notification_email(titre, corps, "Consulter dans ADSUM", lien, signature, site)
    rows = db.fetch_all("SELECT email FROM membre WHERE id = ANY(%s) AND email IS NOT NULL AND email <> ''", (ids,), role=role)
    eligibles = len(rows or [])
    envoyes = 0
    for r in (rows or [])[:_MAX_TELEGRAM]:
        try:
            ok, _ = email_gateway.send_email(str(r["email"]), titre, corps, html)
            if ok:
                envoyes += 1
        except Exception:  # noqa: BLE001 - a broken relay never fails the publish
            continue
    return {"envoyes": envoyes, "eligibles": eligibles, "tronques": max(0, eligibles - _MAX_TELEGRAM)}


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
