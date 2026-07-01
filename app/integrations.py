"""Admin integration settings: view (masked) and rotate channel tokens, and read
built-in guidance.

Lets an administrator replace a compromised Telegram bot token immediately (for
example after a leak or an intrusion) without a redeployment, and see the live
status of each notification channel. Values are secret: they are returned masked,
only admins may read or change them, and every change is audited.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, channels, db
from .config import settings
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/integrations", tags=["integrations"])

require_admin = require_roles("super_admin", "admin")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "direction")

# Guidance shown next to each setting (rendered behind info icons in the UI).
_GUIDE = {
    "telegram_bot_token": {
        "titre": "Jeton du bot Telegram",
        "aide": "Jeton d'acces du bot @adsum_sr_bot. Il autorise l'envoi des notifications Telegram.",
        "obtenir": "Dans Telegram, ouvrez @BotFather, envoyez /mybots, choisissez le bot, puis API Token pour voir le jeton.",
        "roter": "En cas de fuite ou d'intrusion : @BotFather > /mybots > le bot > API Token > Revoke current token. BotFather genere un nouveau jeton ; collez-le ici et enregistrez. L'ancien cesse aussitot de fonctionner.",
    },
    "telegram_bot_username": {
        "titre": "Identifiant du bot Telegram",
        "aide": "Nom d'utilisateur du bot (sans @), utilise pour les liens de liaison des membres.",
        "obtenir": "C'est le username choisi a la creation (ex. adsum_sr_bot).",
        "roter": "Modifiable via @BotFather (/setname ne change pas le username ; un nouveau username se fait via BotFather).",
    },
    "signature": {
        "titre": "Signature des messages",
        "aide": "Texte de signature ajoute a la fin de chaque notification. Par defaut : Sacerdoce Royal.",
        "roter": "Modifiez ici pour personnaliser la signature de tous les messages.",
    },
    "site_officiel": {
        "titre": "Site officiel",
        "aide": "Adresse du site officiel affichee en pied de chaque message.",
        "roter": "Renseignez l'URL officielle (ex. sacerdoceroyal.info).",
    },
}


class ValeurIn(BaseModel):
    valeur: str


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[0] + "..." if value else ""
    return f"{value[:6]}...{value[-4:]}"


@router.get("")
def list_integrations(user: Annotated[UserMe, Depends(require_admin)]) -> list[dict[str, object]]:
    rows = db.fetch_all("SELECT cle, valeur, categorie, maj_le FROM integration_config ORDER BY categorie, cle", (), role=user.role)
    out: list[dict[str, object]] = []
    for r in rows:
        g = _GUIDE.get(r["cle"], {})
        out.append({
            "cle": r["cle"],
            "categorie": r["categorie"],
            "valeur_masquee": _mask(r["valeur"]),
            "renseigne": bool(r["valeur"]),
            "maj_le": r["maj_le"].isoformat() if r["maj_le"] else None,
            "guide": g,
        })
    return out


@router.put("/{cle}")
def set_integration(cle: str, payload: ValeurIn, user: Annotated[UserMe, Depends(require_admin)]) -> dict[str, object]:
    exists = db.fetch_one("SELECT cle FROM integration_config WHERE cle = %s", (cle,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown setting")
    db.execute(
        "UPDATE integration_config SET valeur = %s, maj_par = %s, maj_le = now() WHERE cle = %s",
        (payload.valeur.strip() or None, user.id, cle),
        role=user.role,
    )
    audit.log(user.id, user.role, "maj_integration", "integration_config", cle, {})
    return {"ok": True, "valeur_masquee": _mask(payload.valeur.strip())}


@router.get("/statut")
def statut_canaux(user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, object]:
    """Live status of each notification channel, with a Telegram bot health check."""
    telegram_ok = False
    telegram_bot = None
    token = channels.telegram_token()
    if token:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(f"{settings.telegram_api_base}/bot{token}/getMe", timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
                telegram_ok = bool(data.get("ok"))
                telegram_bot = (data.get("result") or {}).get("username")
        except Exception:  # noqa: BLE001
            telegram_ok = False
    return {
        "in_app": {"actif": True, "note": "Toujours actif."},
        "email": {"actif": settings.email_provider not in ("", "console"), "provider": settings.email_provider, "note": "Livraison a toute adresse possible uniquement apres verification du domaine (SPF/DKIM)."},
        "telegram": {"actif": telegram_ok, "bot": telegram_bot, "gratuit": True, "note": "Canal gratuit. Chaque membre doit lier son compte (Demarrer sur le bot)."},
        "whatsapp": {"actif": channels.whatsapp_configured(), "gratuit": False, "note": "Payant par message (Meta Cloud API), necessite un compte WABA verifie et des modeles approuves."},
        "sms": {"actif": channels.sms_configured(), "gratuit": False, "note": "Aucun fournisseur configure (payant)."},
    }
