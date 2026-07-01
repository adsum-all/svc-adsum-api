"""Automatic birthday wishes and its admin-editable message.

A daily cron (secured by a shared secret Vercel sends as a bearer header) finds
the members whose birthday (day and month of their birth date) is today, and
sends them a message over every opted-in channel: in-app, e-mail, and Telegram or
WhatsApp when configured. Each member is wished once per year (idempotent). The
administration can edit the message text and image; a default is seeded.

Also holds the member channel-linking endpoints (Telegram chat id, WhatsApp
number) so the birthday and other notifications can reach those channels.
"""
# ruff: noqa: E501
from __future__ import annotations

import secrets
import urllib.request
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from . import audit, channels, db
from .auth import current_user
from .config import settings
from .cron_auth import require_cron_auth
from .deps import require_roles
from .email_templates import render_anniversaire_email
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["anniversaires"])

require_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


# --- Admin: birthday message template ---------------------------------------

class ModeleIn(BaseModel):
    titre: str
    corps: str
    image_url: str | None = None
    actif: bool = True


@router.get("/admin/modeles/anniversaire")
def get_modele(user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, object]:
    row = db.fetch_one("SELECT titre, corps, image_url, actif FROM modele_message WHERE cle = 'anniversaire'", (), role=user.role)
    if not row:
        return {"titre": "", "corps": "", "image_url": None, "actif": True}
    return {"titre": row["titre"], "corps": row["corps"], "image_url": row["image_url"], "actif": bool(row["actif"])}


@router.put("/admin/modeles/anniversaire")
def set_modele(payload: ModeleIn, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    if not payload.titre.strip() or not payload.corps.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="titre and corps required")
    db.execute(
        """
        INSERT INTO modele_message (cle, titre, corps, image_url, actif, maj_par, maj_le)
        VALUES ('anniversaire', %s, %s, %s, %s, %s, now())
        ON CONFLICT (cle) DO UPDATE SET titre = EXCLUDED.titre, corps = EXCLUDED.corps,
            image_url = EXCLUDED.image_url, actif = EXCLUDED.actif, maj_par = EXCLUDED.maj_par, maj_le = now()
        """,
        (payload.titre, payload.corps, payload.image_url or None, payload.actif, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "config_modele_anniversaire", "modele_message", "anniversaire", {})
    return {"ok": True}


# --- Daily cron: send birthday wishes ---------------------------------------

def _run_anniversaires(role: str | None) -> dict[str, object]:
    modele = db.fetch_one("SELECT titre, corps, image_url, actif FROM modele_message WHERE cle = 'anniversaire'", (), role=role)
    if not modele or not modele["actif"]:
        return {"ok": True, "envoyes": 0, "raison": "modele_inactif"}
    # Members whose birthday (month/day) is today and not yet wished this year.
    rows = db.fetch_all(
        """
        SELECT m.id, m.prenoms, EXTRACT(YEAR FROM now())::int AS annee
        FROM membre m
        WHERE m.date_naissance IS NOT NULL
          AND EXTRACT(MONTH FROM m.date_naissance) = EXTRACT(MONTH FROM now())
          AND EXTRACT(DAY FROM m.date_naissance) = EXTRACT(DAY FROM now())
          AND m.statut = 'actif'
          AND NOT EXISTS (
            SELECT 1 FROM notification_anniversaire na
            WHERE na.membre_id = m.id AND na.annee = EXTRACT(YEAR FROM now())::int
          )
        """,
        (),
        role=role,
    )
    envoyes = 0
    for r in rows:
        # Reserve the (member, year) slot BEFORE sending. If another concurrent
        # run already claimed it, the RETURNING yields no row and we skip: the
        # unique constraint guarantees a single wish per member per year even
        # under overlapping cron executions.
        reserved = db.fetch_one(
            "INSERT INTO notification_anniversaire (membre_id, annee, canaux) VALUES (%s, %s, '') "
            "ON CONFLICT (membre_id, annee) DO NOTHING RETURNING id",
            (str(r["id"]), int(r["annee"])),
            role=role,
        )
        if not reserved:
            continue
        prenom = (r["prenoms"] or "cher membre").split(" ")[0]
        titre = str(modele["titre"]).replace("{prenom}", prenom)
        corps = str(modele["corps"]).replace("{prenom}", prenom)
        html = render_anniversaire_email(titre, corps, modele["image_url"])
        msg = channels.Message(titre=titre, corps_text=corps, corps_html=html, image_url=modele["image_url"], type_notif="anniversaire")
        used = channels.dispatch(str(r["id"]), role, msg, whatsapp_params=[prenom])
        db.execute(
            "UPDATE notification_anniversaire SET canaux = %s WHERE membre_id = %s AND annee = %s",
            (",".join(used), str(r["id"]), int(r["annee"])),
            role=role,
        )
        envoyes += 1
    return {"ok": True, "envoyes": envoyes}


@router.get("/cron/anniversaires")
def cron_anniversaires(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    """Daily birthday job. Secured by the CRON_SECRET bearer that Vercel sends.

    Vercel injects ``Authorization: Bearer $CRON_SECRET`` (the un-prefixed env var)
    on scheduled invocations; we also accept ADSUM_CRON_SECRET for parity.
    """
    require_cron_auth(authorization)
    return _run_anniversaires(role=None)


@router.post("/admin/anniversaires/declencher")
def declencher_manuel(user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    """Manually run the birthday job (admin), for testing or a missed day."""
    result = _run_anniversaires(role=user.role)
    audit.log(user.id, user.role, "declenchement_anniversaires", "membre", None, result)
    return result


# --- Member channel linking -------------------------------------------------

class WhatsappIn(BaseModel):
    numero: str  # E.164, with or without +


@router.post("/membres/me/telegram/lien")
def telegram_lien(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Return a Telegram deep link the member opens to link their account."""
    membre_id, role = ctx
    username = channels.telegram_username()
    if not username:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="telegram not configured")
    # High-entropy, single-use secret. It travels only inside the deep link the
    # member opens; it is never returned separately in the response body, so it
    # cannot leak into logs and let a third party pre-claim the chat via getUpdates.
    token = secrets.token_urlsafe(24)
    db.execute("UPDATE membre SET telegram_link_token = %s WHERE id = %s", (token, membre_id), role=role)
    return {"deep_link": f"https://t.me/{username}?start={token}"}


@router.post("/membres/me/telegram/verifier")
def telegram_verifier(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """After the member pressed Start, capture their chat id via getUpdates."""
    membre_id, role = ctx
    if not channels.telegram_token():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="telegram not configured")
    row = db.fetch_one("SELECT telegram_link_token FROM membre WHERE id = %s", (membre_id,), role=role)
    token = row["telegram_link_token"] if row else None
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no pending link; request a link first")
    chat_id = _find_chat_id_for_token(str(token))
    if not chat_id:
        return {"linked": False, "message": "Ouvrez le lien et appuyez sur Démarrer, puis réessayez."}
    db.execute("UPDATE membre SET telegram_chat_id = %s, telegram_link_token = NULL WHERE id = %s", (chat_id, membre_id), role=role)
    db.execute(
        "INSERT INTO preference_notification (membre_id, telegram) VALUES (%s, true) "
        "ON CONFLICT (membre_id) DO UPDATE SET telegram = true",
        (membre_id,),
        role=role,
    )
    return {"linked": True}


def _find_chat_id_for_token(token: str) -> str | None:
    """Scan recent bot updates for the '/start <token>' message; return its chat id."""
    url = f"{settings.telegram_api_base}/bot{channels.telegram_token()}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - fixed Telegram host
            import json

            data = json.load(resp)
    except Exception:  # noqa: BLE001
        return None
    for update in reversed(data.get("result", [])):
        message = update.get("message") or {}
        text = message.get("text") or ""
        if text.strip() == f"/start {token}":
            chat = message.get("chat") or {}
            cid = chat.get("id")
            return str(cid) if cid is not None else None
    return None


@router.put("/membres/me/whatsapp")
def set_whatsapp(payload: WhatsappIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Register the member's WhatsApp number (E.164) and opt into the channel."""
    membre_id, role = ctx
    numero = payload.numero.strip()
    if len(numero.lstrip("+")) < 8 or not numero.lstrip("+").isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid phone number")
    db.execute("UPDATE membre SET whatsapp_numero = %s WHERE id = %s", (numero, membre_id), role=role)
    db.execute(
        "INSERT INTO preference_notification (membre_id, whatsapp) VALUES (%s, true) "
        "ON CONFLICT (membre_id) DO UPDATE SET whatsapp = true",
        (membre_id,),
        role=role,
    )
    return {"ok": True}
