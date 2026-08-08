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
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/integrations", tags=["integrations"])


# Guidance shown next to each setting (rendered behind info icons in the UI).
_GUIDE = {
    "org_marque": {
        "titre": "Nom affiché dans les e-mails",
        "aide": "Le nom en grand dans l'en-tête des messages, et dans leurs objets. C'est la marque de la plateforme telle que vos membres la voient.",
        "obtenir": "Choisissez le nom sous lequel votre organisation communique. Laissez vide pour conserver ADSUM.",
    },
    "org_couleur_principale": {
        "titre": "Couleur principale des e-mails",
        "aide": "La couleur de l'en-tête, des boutons et des accents dans les messages. Format hexadécimal, par exemple #2a4fad.",
        "obtenir": "Reprenez la couleur de votre charte. Une valeur invalide est ignorée et la couleur d'origine est conservée.",
    },
    "org_couleur_sombre": {
        "titre": "Couleur sombre des e-mails",
        "aide": "La seconde couleur du dégradé d'en-tête. Format hexadécimal, par exemple #1d3470.",
        "obtenir": "Prenez une teinte plus sombre de votre couleur principale.",
    },
    "org_baseline": {
        "titre": "Phrase de pied de page",
        "aide": "La phrase qui décrit votre organisation en bas de chaque message, par exemple « plateforme d'adhésion et de présence ».",
        "obtenir": "Une phrase courte, sans point final : la ville et le nom sont ajoutés automatiquement.",
    },
    "email_webhook_secret": {
        "titre": "Clé du retour de livraison des e-mails",
        "aide": "Elle authentifie les retours du fournisseur d'e-mail (livré, ouvert, rejeté). Sans elle, la plateforme ne sait pas ce que devient un message une fois parti.",
        "obtenir": "Générez une valeur longue et aléatoire, puis collez l'adresse de rappel affichée ci-dessous dans Brevo, rubrique Transactionnel puis Paramètres puis Webhooks.",
    },
    "telegram_bot_token": {
        "titre": "Jeton du bot Telegram",
        "aide": "Jeton d'accès du bot @adsum_sr_bot. Il autorise l'envoi des notifications Telegram.",
        "obtenir": "Dans Telegram, ouvrez @BotFather, envoyez /mybots, choisissez le bot, puis API Token pour voir le jeton.",
        "roter": "En cas de fuite ou d'intrusion : @BotFather > /mybots > le bot > API Token > Revoke current token. BotFather génère un nouveau jeton ; collez-le ici et enregistrez. L'ancien cesse aussitôt de fonctionner.",
    },
    "telegram_bot_username": {
        "titre": "Identifiant du bot Telegram",
        "aide": "Nom d'utilisateur du bot (sans @), utilisé pour les liens de liaison des membres.",
        "obtenir": "C'est le username choisi à la création (ex. adsum_sr_bot).",
        "roter": "Modifiable via @BotFather (/setname ne change pas le username ; un nouveau username se fait via BotFather).",
    },
    "signature": {
        "titre": "Signature des messages",
        "aide": "Texte de signature ajouté à la fin de chaque notification. Par défaut : Sacerdoce Royal.",
        "roter": "Modifiez ici pour personnaliser la signature de tous les messages.",
    },
    "signature_anniversaire": {
        "titre": "Signature des messages d'anniversaire",
        "aide": "Signature spécifique aux messages d'anniversaire. Vide = signature globale.",
        "roter": "Exemple : Fraternellement, Sacerdoce Royal.",
    },
    "signature_information": {
        "titre": "Signature des messages d'information",
        "aide": "Signature des annonces, agendas et récapitulatifs. Vide = signature globale.",
        "roter": "Exemple : Sacerdoce Royal.",
    },
    "signature_accuse": {
        "titre": "Signature des accusés de réception",
        "aide": "Signature des confirmations de réception (dossier d'adhésion reçu). Vide = signature globale.",
        "roter": "Exemple : Sacerdoce Royal.",
    },
    "signature_approbation": {
        "titre": "Signature des messages d'approbation",
        "aide": "Signature des décisions favorables (adhésion validée, attestation requise). Vide = signature globale.",
        "roter": "Exemple : Fraternellement, Sacerdoce Royal.",
    },
    "signature_correction": {
        "titre": "Signature des demandes de correction",
        "aide": "Signature des messages demandant une correction du dossier. Vide = signature globale.",
        "roter": "Exemple : Sacerdoce Royal.",
    },
    "signature_rappel": {
        "titre": "Signature des rappels",
        "aide": "Signature des rappels (attestation, activités). Vide = signature globale.",
        "roter": "Exemple : Sacerdoce Royal.",
    },
    "signature_convocation": {
        "titre": "Signature des convocations (sondage de pointage, début d'activité)",
        "aide": "Signature des convocations : sondage de pointage (présence) et rappel de début d'activité. Vide = signature globale.",
        "roter": "Exemples : Le Modérateur ; Le Collège des Bergers ; L'Administration.",
    },
    "site_officiel": {
        "titre": "Site officiel",
        "aide": "Adresse du site officiel affichée en pied de chaque message.",
        "roter": "Renseignez l'URL officielle (ex. sacerdoceroyal.info).",
    },
    "email_from": {
        "titre": "Adresse expéditrice des e-mails",
        "aide": "Adresse unique qui envoie tous les e-mails (mot de passe temporaire, notifications). Par défaut : saintgabrielsacerdoceroyal@ikmail.com.",
        "obtenir": "C'est l'adresse qui apparaîtra comme expéditeur. Elle DOIT être validée comme expéditeur chez le fournisseur (Brevo).",
        "roter": "Pour changer d'adresse expéditrice : 1) ajoutez et validez la nouvelle adresse chez Brevo (Expéditeurs > Ajouter, puis cliquez le lien de confirmation reçu). 2) Mettez l'adresse ici. 3) Envoyez un e-mail de test (bouton ci-dessous) pour vérifier la réception. Si vous passez à une adresse sur votre propre domaine, configurez aussi SPF/DKIM/DMARC chez Brevo pour une bonne délivrabilité.",
    },
    "email_from_name": {
        "titre": "Nom expéditeur affiché",
        "aide": "Nom affiché à côté de l'adresse (ex. Sacerdoce Royal).",
        "roter": "Modifiez ici le nom affiché des e-mails.",
    },
    "email_provider": {
        "titre": "Fournisseur d'e-mail",
        "aide": "Service qui envoie les e-mails : brevo (recommandé), resend, smtp, ou console (dev, n'envoie rien). Plusieurs valeurs séparées par des virgules = repli automatique (ex. brevo,resend).",
        "roter": "Choisissez le fournisseur. Avec Brevo : vérifiez que la sécurité 'Authorised IPs' est DÉSACTIVÉE dans Brevo (sinon les envois depuis le serveur sont bloqués), et que la clé API renseignée est une clé API v3 (xkeysib-...), pas une clé SMTP.",
    },
    "email_api_key": {
        "titre": "Clé API du fournisseur d'e-mail",
        "aide": "Clé API du fournisseur (Brevo : xkeysib-... ; Resend : re_...). Stockée de façon sécurisée, affichée masquée.",
        "roter": "Collez ici la nouvelle clé API et enregistrez. Pour Brevo, prenez la clé dans Paramètres > Clés API (PAS la clé SMTP). Après changement, envoyez un e-mail de test.",
    },
    "email_smtp_host": {
        "titre": "Serveur SMTP (envoi via votre boîte mail)",
        "aide": "Serveur d'envoi de votre fournisseur de boîte mail. Infomaniak : mail.infomaniak.com. Bouygues : smtp.bbox.fr. Free : smtp.free.fr. Utilisé quand email_provider contient smtp. Attention : un fournisseur d'accès impose un quota journalier bas, n'accepte d'envoyer que depuis sa propre adresse (voir Expéditeur SMTP) et ne remonte aucun statut de livraison. Convient au secours, pas à un envoi général.",
        "roter": "Renseignez le serveur SMTP de votre fournisseur (Gmail : smtp.gmail.com ; Infomaniak : mail.infomaniak.com).",
    },
    "email_smtp_port": {
        "titre": "Port SMTP",
        "aide": "465 (SSL, recommandé) ou 587 (STARTTLS).",
        "roter": "Mettez 465 en priorité ; si bloqué, essayez 587.",
    },
    "email_smtp_user": {
        "titre": "Identifiant SMTP",
        "aide": "En général l'adresse e-mail complète (ex. saintgabrielsacerdoceroyal@ikmail.com).",
        "roter": "C'est l'adresse de la boîte qui envoie.",
    },
    "email_reply_to": {
        "libelle": "Adresse de réponse",
        "aide": "Où arrivent les réponses des membres. Laissez vide et elles tombent dans la boîte d'envoi, que personne ne lit. Renseignez une adresse réellement relevée.",
    },
    "email_api_key_brevo": {
        "libelle": "Clé API Brevo",
        "aide": "Clé propre à Brevo (commence par xkeysib). Distincte de la clé SMTP Brevo. Renseignez-la pour pouvoir armer une chaîne de secours vers un autre fournisseur.",
    },
    "email_api_key_resend": {
        "libelle": "Clé API Resend",
        "aide": "Clé propre à Resend. Chaque fournisseur a désormais sa clé : sans cela, une chaîne « brevo,resend » enverrait la clé Brevo à Resend, qui la refuserait.",
    },
    "email_smtp_from": {
        "libelle": "Expéditeur SMTP",
        "aide": "À renseigner si le serveur SMTP n'accepte d'envoyer que depuis sa propre adresse, ce qui est le cas des fournisseurs d'accès (Bouygues, Free). Laissez vide pour utiliser l'expéditeur habituel.",
    },
    "email_smtp_password": {
        "titre": "Mot de passe SMTP",
        "aide": "Mot de passe de la boîte mail (ou mot de passe d'application si la double authentification est active chez le fournisseur). Stocké masqué.",
        "roter": "Chez Infomaniak : activez l'accès IMAP/SMTP dans les paramètres de la boîte ; si 2FA active, générez un mot de passe d'application dédié. Collez-le ici puis envoyez un e-mail de test.",
    },
}


# Curated authorities the admin can pick as a signature (quick-picks in the UI).
# The field stays free text, but these cover the sanctioned organs so a signature
# is chosen from a coherent list rather than typed differently each time.
SIGNATURES_SUGGEREES = [
    "Le Modérateur",
    "Le Collège des Bergers",
    "L'Administration",
    "La Direction",
    "Le Sacerdoce Royal",
    "Le Bureau du Sacerdoce Royal",
    "Fraternellement, le Sacerdoce Royal",
]


class ValeurIn(BaseModel):
    valeur: str


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[0] + "..." if value else ""
    return f"{value[:6]}...{value[-4:]}"


@router.get("")
def list_integrations(user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))]) -> list[dict[str, object]]:
    rows = db.fetch_all("SELECT cle, valeur, categorie, maj_le FROM integration_config ORDER BY categorie, cle", (), role=user.role)
    out: list[dict[str, object]] = []
    for r in rows:
        g = _GUIDE.get(r["cle"], {})
        est_signature = str(r["cle"]).startswith("signature")
        # A signature or the public site URL is not a secret: show it in clear so
        # the admin sees the exact organ / address; tokens stay masked.
        en_clair = est_signature or str(r["cle"]) == "site_officiel"
        out.append({
            "cle": r["cle"],
            "categorie": r["categorie"],
            "valeur_masquee": (r["valeur"] or "") if en_clair else _mask(r["valeur"]),
            "renseigne": bool(r["valeur"]),
            "maj_le": r["maj_le"].isoformat() if r["maj_le"] else None,
            "guide": g,
            "suggestions": SIGNATURES_SUGGEREES if est_signature else [],
        })
    return out


@router.put("/{cle}")
def set_integration(cle: str, payload: ValeurIn, user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))]) -> dict[str, object]:
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
def statut_canaux(user: Annotated[UserMe, Depends(require_permission("integrations.superviser"))]) -> dict[str, object]:
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
        "in_app": {"actif": True, "autorise": True, "verrouille": True, "note": "Toujours actif."},
        "email": {"actif": settings.email_provider not in ("", "console"), "autorise": channels.canal_actif("email"), "provider": settings.email_provider, "note": "Livraison à toute adresse possible uniquement après vérification du domaine (SPF/DKIM)."},
        "telegram": {"actif": telegram_ok, "autorise": channels.canal_actif("telegram"), "bot": telegram_bot, "gratuit": True, "note": "Canal gratuit. Chaque membre doit lier son compte (Démarrer sur le bot)."},
        "whatsapp": {"actif": channels.whatsapp_configured(), "autorise": channels.canal_actif("whatsapp"), "gratuit": False, "note": "Payant par message (Meta Cloud API), nécessite un compte WABA vérifié et des modèles approuvés."},
        "sms": {"actif": channels.sms_configured(), "autorise": channels.canal_actif("sms"), "gratuit": False, "note": "Aucun fournisseur configuré (payant)."},
    }


class TestEmailIn(BaseModel):
    to: EmailStr | None = None


@router.post("/test-email")
def test_email(payload: TestEmailIn, user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))]) -> dict[str, object]:
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
