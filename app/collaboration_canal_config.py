# ruff: noqa: E501 - long SQL statements
"""Dedicated instruction-bot configuration workflow (per general workspace).

Each general workspace owns a dedicated Telegram instruction bot, separate from the
member notification bot. Since Telegram forbids creating a bot programmatically, a
gerant of the workspace RAISES a configuration request; the back-office team then
creates the bot in BotFather and pastes its token/username here. Documented steps
are returned so the back-office knows exactly what to do.
"""
from __future__ import annotations

import json
import re
import urllib.request
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, crypto, db
from .collaboration_espaces import GERANTS, require_espace_role
from .config import settings
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["collaboration-canal-config"])

# The steps the back-office follows to provision a workspace's instruction bot.
ETAPES_CONFIG = [
    "Ouvrir Telegram et demarrer une conversation avec @BotFather.",
    "Envoyer /newbot, puis choisir un nom lisible (ex: 'ADSUM Instructions - <espace>').",
    "Choisir un identifiant unique se terminant par 'bot' (ex: adsum_instr_<espace>_bot).",
    "BotFather renvoie un token du type 123456:ABC-...; le copier.",
    "Coller l'identifiant (username sans @) et le token ci-dessous, puis valider.",
    "Le membre autorise scanne alors le QR de l'espace d'instruction pour demarrer le bot.",
]


class DemandeOut(BaseModel):
    ok: bool = True
    statut: str


class BotConfigIn(BaseModel):
    username: str
    token: str


def chiffrer_token(token: str) -> str:
    """Encrypt a BotFather token for storage (Fernet, like the AI provider keys), so a
    dump of the collab_espace table never exposes a usable bot token. Stored as text
    (the Fernet ciphertext is URL-safe base64)."""
    return crypto.encrypt_bytes(token.encode()).decode("ascii")


def dechiffrer_token(stored: str | None) -> str:
    """Return the usable token from what is stored. A real BotFather token contains a
    ':' (format 123:ABC) and is a legacy plain value kept working as-is; anything else
    is a Fernet ciphertext and is decrypted. Backward compatible, no migration needed."""
    if not stored:
        return ""
    if ":" in stored:
        return stored  # legacy plain token
    try:
        return crypto.decrypt_bytes(stored.encode()).decode("utf-8")
    except Exception:  # noqa: BLE001 - never leak a crypto error; fall back to stored
        return stored


def _revoquer_webhook(token: str) -> bool:
    """Best-effort revoke: tell Telegram to drop any webhook on this bot so no stale
    endpoint keeps receiving updates once the bot is dissociated. Never raises (a
    network failure must not block the local dissociation). Returns True on success.
    Note: the Telegram Bot API cannot DELETE a bot; only @BotFather /deletebot can."""
    if not token or ":" not in token:
        return False
    url = f"{settings.telegram_api_base}/bot{token}/deleteWebhook?drop_pending_updates=false"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - fixed Telegram host
            data = json.load(resp)
        return bool(data.get("ok"))
    except Exception:  # noqa: BLE001 - revocation is best-effort, never fatal
        return False


@router.post("/collaboration/espaces/{espace_id}/canal/demander-config", response_model=DemandeOut)
def demander_config(
    espace_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> DemandeOut:
    """A workspace gerant raises a request for the back-office to provision the
    dedicated instruction bot of this workspace's instruction space."""
    eid = str(espace_id)
    require_espace_role(eid, user, GERANTS)
    row = db.fetch_one("SELECT parent_id, instruction_bot_statut FROM collab_espace WHERE id = %s", (eid,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="espace introuvable")
    if row.get("parent_id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un sous-espace partage le bot de son espace general")
    if row.get("instruction_bot_statut") == "configure":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="le bot de cet espace est deja configure")
    db.execute(
        "UPDATE collab_espace SET instruction_bot_statut = 'demande', instruction_config_demande_le = now() WHERE id = %s",
        (eid,), role=user.role,
    )
    audit.log(user.id, user.role, "canal_bot_demande_config", "collab_espace", eid, {})
    return DemandeOut(statut="demande")


@router.get("/admin/collaboration/bot-demandes")
def bot_demandes(
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, Any]:
    """Back-office view: workspaces awaiting a dedicated instruction bot, with the
    documented provisioning steps."""
    rows = db.fetch_all(
        "SELECT id, nom, instruction_bot_statut, instruction_bot_username, instruction_config_demande_le "
        "FROM collab_espace WHERE parent_id IS NULL AND NOT archive AND supprime_le IS NULL "
        "AND instruction_bot_statut IN ('demande', 'non_configure') ORDER BY instruction_bot_statut DESC, nom",
        (), role=user.role,
    )
    return {
        "etapes": ETAPES_CONFIG,
        "espaces": [
            {"id": str(r["id"]), "nom": r["nom"], "statut": r["instruction_bot_statut"],
             "username": r["instruction_bot_username"],
             "demande_le": r["instruction_config_demande_le"].isoformat() if r["instruction_config_demande_le"] else None}
            for r in rows
        ],
    }


@router.put("/admin/collaboration/espaces/{espace_id}/bot")
def configurer_bot(
    espace_id: uuid.UUID,
    payload: BotConfigIn,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, Any]:
    """Back-office (super-admin) sets the dedicated instruction bot of a workspace.
    Marks it configured, which enables its deep link, QR and voice-note ingestion."""
    eid = str(espace_id)
    username = payload.username.strip().lstrip("@")
    token = payload.token.strip()
    # Strict BotFather token shape (digits ':' alnum/_/-): also prevents any path or
    # query injection into the fixed Telegram URL used at webhook revocation time.
    if not username or not re.fullmatch(r"\d+:[\w-]+", token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username et token du bot requis (token BotFather du type 123:ABC)")
    row = db.execute(
        "UPDATE collab_espace SET instruction_bot_username = %s, instruction_bot_token = %s, "
        "instruction_bot_statut = 'configure', instruction_config_par = %s WHERE id = %s AND parent_id IS NULL RETURNING id",
        (username, chiffrer_token(token), user.id, eid), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="espace general introuvable")
    audit.log(user.id, user.role, "canal_bot_configure", "collab_espace", eid, {"username": username})
    return {"ok": True, "statut": "configure", "username": username}


@router.get("/admin/collaboration/bots")
def liste_bots(
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, Any]:
    """Back-office view of every configured instruction bot (one per general
    workspace), so it can be seen and dissociated. Tokens are never returned."""
    rows = db.fetch_all(
        "SELECT id, nom, instruction_bot_username, instruction_bot_statut, "
        "instruction_derniere_synchro, instruction_derniere_note, instruction_dernier_echec, instruction_dernier_echec_le "
        "FROM collab_espace WHERE parent_id IS NULL AND supprime_le IS NULL "
        "AND instruction_bot_statut = 'configure' ORDER BY nom",
        (), role=user.role,
    )

    def _iso(v: Any) -> str | None:
        return v.isoformat() if v is not None and hasattr(v, "isoformat") else None

    return {
        "bots": [
            {"espace_id": str(r["id"]), "nom": r["nom"], "username": r["instruction_bot_username"],
             "statut": r["instruction_bot_statut"],
             "derniere_synchro": _iso(r["instruction_derniere_synchro"]),
             "derniere_note": _iso(r["instruction_derniere_note"]),
             "dernier_echec": r["instruction_dernier_echec"],
             "dernier_echec_le": _iso(r["instruction_dernier_echec_le"])}
            for r in rows
        ],
    }


@router.delete("/admin/collaboration/espaces/{espace_id}/bot")
def dissocier_bot(
    espace_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, Any]:
    """Dissociate a workspace's dedicated instruction bot: revoke any webhook
    (best-effort), then clear its token/username and set the status back to
    'non_configure'. The QR and deep link deactivate at once and voice-note
    ingestion stops for this workspace.

    Honest boundary: the Telegram Bot API cannot DELETE a bot. This endpoint never
    claims the Telegram bot is deleted; to remove it for good, an administrator must
    run /deletebot in @BotFather. The response says so explicitly."""
    eid = str(espace_id)
    row = db.fetch_one(
        "SELECT instruction_bot_token, instruction_bot_username FROM collab_espace "
        "WHERE id = %s AND parent_id IS NULL", (eid,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="espace general introuvable")
    ancien_token = dechiffrer_token(row.get("instruction_bot_token"))
    ancien_username = row.get("instruction_bot_username")
    webhook_revoque = _revoquer_webhook(ancien_token) if ancien_token else False
    db.execute(
        "UPDATE collab_espace SET instruction_bot_token = NULL, instruction_bot_username = NULL, "
        "instruction_bot_statut = 'non_configure', instruction_config_demande_le = NULL, "
        "instruction_config_par = NULL WHERE id = %s",
        (eid,), role=user.role,
    )
    audit.log(user.id, user.role, "canal_bot_dissocie", "collab_espace", eid,
              {"ancien_username": ancien_username, "webhook_revoque": webhook_revoque})
    return {
        "ok": True,
        "statut": "non_configure",
        "webhook_revoque": webhook_revoque,
        "message": (
            "Bot dissocie de l'espace: le lien, le QR et la remontee des notes sont desactives. "
            "Le bot Telegram existe toujours cote Telegram; pour le supprimer definitivement, "
            "utilisez /deletebot dans @BotFather."
        ),
    }
