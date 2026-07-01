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
from pydantic import BaseModel, EmailStr

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
    "email_from": {
        "titre": "Adresse expeditrice des e-mails",
        "aide": "Adresse unique qui envoie tous les e-mails (mot de passe temporaire, notifications). Par defaut : saintgabrielsacerdoceroyal@ikmail.com.",
        "obtenir": "C'est l'adresse qui apparaitra comme expediteur. Elle DOIT etre validee comme expediteur chez le fournisseur (Brevo).",
        "roter": "Pour changer d'adresse expeditrice : 1) ajoutez et validez la nouvelle adresse chez Brevo (Expediteurs > Ajouter, puis cliquez le lien de confirmation recu). 2) Mettez l'adresse ici. 3) Envoyez un e-mail de test (bouton ci-dessous) pour verifier la reception. Si vous passez a une adresse sur votre propre domaine, configurez aussi SPF/DKIM/DMARC chez Brevo pour une bonne delivrabilite.",
    },
    "email_from_name": {
        "titre": "Nom expediteur affiche",
        "aide": "Nom affiche a cote de l'adresse (ex. Sacerdoce Royal).",
        "roter": "Modifiez ici le nom affiche des e-mails.",
    },
    "email_provider": {
        "titre": "Fournisseur d'e-mail",
        "aide": "Service qui envoie les e-mails : brevo (recommande), resend, smtp, ou console (dev, n'envoie rien). Plusieurs valeurs separees par des virgules = repli automatique (ex. brevo,resend).",
        "roter": "Choisissez le fournisseur. Avec Brevo : verifiez que la securite 'Authorised IPs' est DESACTIVEE dans Brevo (sinon les envois depuis le serveur sont bloques), et que la cle API renseignee est une cle API v3 (xkeysib-...), pas une cle SMTP.",
    },
    "email_api_key": {
        "titre": "Cle API du fournisseur d'e-mail",
        "aide": "Cle API du fournisseur (Brevo : xkeysib-... ; Resend : re_...). Stockee de facon securisee, affichee masquee.",
        "roter": "Collez ici la nouvelle cle API et enregistrez. Pour Brevo, prenez la cle dans Parametres > Cles API (PAS la cle SMTP). Apres changement, envoyez un e-mail de test.",
    },
    "email_smtp_host": {
        "titre": "Serveur SMTP (envoi via votre boite mail)",
        "aide": "Serveur d'envoi de votre fournisseur de boite mail. Pour ikmail / ik.me (Infomaniak) : mail.infomaniak.com. Utilise quand email_provider = smtp.",
        "roter": "Renseignez le serveur SMTP de votre fournisseur (Gmail : smtp.gmail.com ; Infomaniak : mail.infomaniak.com).",
    },
    "email_smtp_port": {
        "titre": "Port SMTP",
        "aide": "465 (SSL, recommande) ou 587 (STARTTLS).",
        "roter": "Mettez 465 en priorite ; si bloque, essayez 587.",
    },
    "email_smtp_user": {
        "titre": "Identifiant SMTP",
        "aide": "En general l'adresse e-mail complete (ex. saintgabrielsacerdoceroyal@ikmail.com).",
        "roter": "C'est l'adresse de la boite qui envoie.",
    },
    "email_smtp_password": {
        "titre": "Mot de passe SMTP",
        "aide": "Mot de passe de la boite mail (ou mot de passe d'application si la double authentification est active chez le fournisseur). Stocke masque.",
        "roter": "Chez Infomaniak : activez l'acces IMAP/SMTP dans les parametres de la boite ; si 2FA active, generez un mot de passe d'application dedie. Collez-le ici puis envoyez un e-mail de test.",
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


class TestEmailIn(BaseModel):
    to: EmailStr | None = None


@router.post("/test-email")
def test_email(payload: TestEmailIn, user: Annotated[UserMe, Depends(require_admin)]) -> dict[str, object]:
    """Send a real test e-mail through the configured pipeline and report the outcome.

    Lets an administrator confirm end-to-end that outgoing e-mail actually works
    (or see the exact failure), instead of assuming success.
    """
    from .email_gateway import send_email

    dest = str(payload.to) if payload.to else str(user.email)
    sent, provider = send_email(
        dest,
        "ADSUM, e-mail de test",
        "Ceci est un e-mail de test ADSUM. Si vous le recevez, la configuration d'envoi fonctionne.",
        "<p>Ceci est un e-mail de test ADSUM. Si vous le recevez, la configuration d'envoi fonctionne.</p>",
    )
    audit.log(user.id, user.role, "test_email", "integration_config", "email", {"to": dest, "sent": sent, "provider": provider})
    return {"sent": sent, "provider": provider, "to": dest}
