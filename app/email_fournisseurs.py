"""Change e-mail provider from the back office, in clicks, without a deployment.

The platform must never be captive to one sender. A free tier runs out of credits,
an account gets suspended, a provider blocks the region: when that happens the
organisation has to move the same day, and an administrator cannot be asked to edit
environment variables or wait for a release.

Three guarantees make a switch safe rather than merely possible:

- **Readiness is computed, not assumed.** A provider is offered as activable only
  when every field it actually needs is filled. Activating a provider with an empty
  API key would stop all outgoing mail silently.
- **A provider is tested before it is activated.** :func:`tester` sends through one
  named provider and deliberately does not fall back, so the answer is about that
  provider and nothing else. A test that fell back would report success for a
  provider that cannot send.
- **The active chain is ordered.** The first provider carries the traffic, the next
  ones catch a failure. Since each provider now holds its own credentials, a chain
  no longer hands one provider's key to another.

Only declared keys can be written here: the payload cannot reach an unrelated
setting even if a caller asks for it.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/email", tags=["email-fournisseurs"])

CLE_CHAINE = "email_provider"


class Champ(BaseModel):
    """One configuration field of a provider, as the back office should render it."""

    cle: str
    libelle: str
    aide: str = ""
    secret: bool = False
    requis: bool = True
    exemple: str = ""


class Fournisseur(BaseModel):
    code: str
    libelle: str
    resume: str
    #: Stated plainly, because the cost of learning it after the switch is high.
    limite: str = ""
    champs: list[Champ] = Field(default_factory=list)
    #: One-click host/port presets, so an administrator never types a server name.
    preselections: list[dict[str, str]] = Field(default_factory=list)


# Fields shared by every provider: they describe the identity of the sender, not
# the transport, so they are edited once and survive a switch.
_COMMUNS = [
    Champ(cle="email_from", libelle="Adresse expéditrice", aide="Ce que les membres voient comme expéditeur. Elle doit être autorisée chez le fournisseur choisi.", exemple="adsum.sr@gmail.com"),
    Champ(cle="email_from_name", libelle="Nom affiché", aide="Le nom à côté de l'adresse.", exemple="Sacerdoce Royal"),
    Champ(cle="email_reply_to", libelle="Adresse de réponse", aide="Où arrivent les réponses des membres. Vide, elles tombent dans la boîte d'envoi que personne ne relève.", requis=False, exemple="contact@..."),
]

CATALOGUE: list[Fournisseur] = [
    Fournisseur(
        code="brevo",
        libelle="Brevo",
        resume="Service d'envoi transactionnel. Remonte le statut de chaque message (remis, ouvert, rejeté).",
        limite="Le palier gratuit est plafonné en nombre d'envois par jour. Un envoi général à tous les membres consomme autant de crédits que de destinataires.",
        champs=[Champ(cle="email_api_key_brevo", libelle="Clé API Brevo", aide="Clé API v3, elle commence par xkeysib. Ce n'est pas la clé SMTP.", secret=True, exemple="xkeysib-...")],
    ),
    Fournisseur(
        code="resend",
        libelle="Resend",
        resume="Service d'envoi transactionnel, alternative à Brevo. Demande en général un domaine vérifié.",
        champs=[Champ(cle="email_api_key_resend", libelle="Clé API Resend", aide="Elle commence par re_.", secret=True, exemple="re_...")],
    ),
    Fournisseur(
        code="smtp",
        libelle="Boîte mail (SMTP)",
        resume="Envoi par le serveur de votre propre boîte : fournisseur d'accès (Bouygues, Free, Orange), hébergeur, ou messagerie professionnelle.",
        limite="Un fournisseur d'accès applique un quota journalier bas et non contractuel, n'accepte d'envoyer que depuis sa propre adresse, et ne remonte aucun statut de livraison. Convient au secours et aux messages de service, pas à un envoi général à tous les membres.",
        champs=[
            Champ(cle="email_smtp_host", libelle="Serveur d'envoi", exemple="smtp.bbox.fr"),
            Champ(cle="email_smtp_port", libelle="Port", aide="465 en SSL, 587 en STARTTLS.", exemple="465"),
            Champ(cle="email_smtp_user", libelle="Identifiant", aide="En général l'adresse complète de la boîte.", exemple="mon.compte@bbox.fr"),
            Champ(cle="email_smtp_password", libelle="Mot de passe", aide="Le mot de passe de la boîte, ou un mot de passe d'application si la double authentification est active.", secret=True),
            Champ(cle="email_smtp_from", libelle="Expéditeur imposé", aide="À renseigner quand le serveur n'accepte d'envoyer que depuis sa propre adresse, ce qui est le cas des fournisseurs d'accès. Sinon l'adresse expéditrice commune est utilisée.", requis=False, exemple="mon.compte@bbox.fr"),
        ],
        preselections=[
            {"libelle": "Bouygues Telecom (bbox.fr)", "email_smtp_host": "smtp.bbox.fr", "email_smtp_port": "465"},
            {"libelle": "Free", "email_smtp_host": "smtp.free.fr", "email_smtp_port": "465"},
            {"libelle": "Orange", "email_smtp_host": "smtp.orange.fr", "email_smtp_port": "465"},
            {"libelle": "SFR", "email_smtp_host": "smtp.sfr.fr", "email_smtp_port": "465"},
            {"libelle": "Infomaniak", "email_smtp_host": "mail.infomaniak.com", "email_smtp_port": "465"},
            {"libelle": "Gmail (mot de passe d'application)", "email_smtp_host": "smtp.gmail.com", "email_smtp_port": "465"},
            {"libelle": "Microsoft 365", "email_smtp_host": "smtp.office365.com", "email_smtp_port": "587"},
            {"libelle": "o2switch", "email_smtp_host": "mail.o2switch.net", "email_smtp_port": "465"},
        ],
    ),
    Fournisseur(
        code="console",
        libelle="Aucun envoi (journal)",
        resume="N'envoie rien, écrit seulement dans le journal du serveur. Réservé au développement.",
        limite="Choisi comme fournisseur principal, plus aucun membre ne reçoit de message : ni code de connexion, ni convocation.",
    ),
]

_PAR_CODE = {f.code: f for f in CATALOGUE}
#: Every key this endpoint is allowed to write. Anything else is refused.
_CLES_AUTORISEES = {c.cle for c in _COMMUNS} | {c.cle for f in CATALOGUE for c in f.champs}


def _valeurs() -> dict[str, str]:
    rows = db.fetch_all("SELECT cle, valeur FROM integration_config WHERE cle LIKE 'email%%'", ())
    return {str(r["cle"]): str(r["valeur"] or "") for r in rows}


def _masquer(valeur: str) -> str:
    if not valeur:
        return ""
    return f"{valeur[:4]}...{valeur[-3:]}" if len(valeur) > 12 else "..."


def _chaine_active() -> list[str]:
    brut = _valeurs().get(CLE_CHAINE, "")
    return [n.strip() for n in brut.split(",") if n.strip()]


def _manquants(f: Fournisseur, valeurs: dict[str, str], chaine: list[str] | None = None) -> list[str]:
    """Required fields of this provider that are still empty, named for a human.

    The historical single ``email_api_key`` counts only for the provider that is
    already sending. A base configured before per-provider keys existed keeps
    working and is not told it is broken. Crediting that key to every provider
    would be worse than useless: the stored key belongs to one service, so another
    would be declared ready and then rejected on its first real send.
    """
    actif = (chaine or [])[:1]
    manque: list[str] = []
    for champ in f.champs:
        if not champ.requis:
            continue
        if valeurs.get(champ.cle):
            continue
        if champ.cle.startswith("email_api_key_") and f.code in actif and valeurs.get("email_api_key"):
            continue
        manque.append(champ.libelle)
    return manque


def _etat(f: Fournisseur, valeurs: dict[str, str], chaine: list[str]) -> dict[str, object]:
    manque = _manquants(f, valeurs, chaine)
    rang = chaine.index(f.code) if f.code in chaine else None
    return {
        "code": f.code,
        "libelle": f.libelle,
        "resume": f.resume,
        "limite": f.limite,
        "pret": not manque,
        "manquant": manque,
        "actif": rang == 0,
        "secours": rang is not None and rang > 0,
        "rang": rang,
        "preselections": f.preselections,
        "champs": [
            {
                **c.model_dump(),
                "valeur": "" if c.secret else valeurs.get(c.cle, ""),
                "valeur_masquee": _masquer(valeurs.get(c.cle, "")) if c.secret else valeurs.get(c.cle, ""),
                "renseigne": bool(valeurs.get(c.cle)),
            }
            for c in f.champs
        ],
    }


@router.get("/fournisseurs")
def lister(user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))]) -> dict[str, object]:
    """Everything the switching screen needs, in one call."""
    valeurs = _valeurs()
    chaine = _chaine_active()
    return {
        "chaine": chaine,
        "commun": [
            {**c.model_dump(), "valeur": valeurs.get(c.cle, ""), "renseigne": bool(valeurs.get(c.cle))}
            for c in _COMMUNS
        ],
        "fournisseurs": [_etat(f, valeurs, chaine) for f in CATALOGUE],
    }


class ValeursIn(BaseModel):
    valeurs: dict[str, str]


def _enregistrer(valeurs: dict[str, str], user: UserMe, contexte: str) -> list[str]:
    refuses = [c for c in valeurs if c not in _CLES_AUTORISEES]
    if refuses:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"réglages non autorisés : {', '.join(sorted(refuses))}")
    ecrits: list[str] = []
    for cle, brut in valeurs.items():
        valeur = brut.strip()
        # An empty secret means "leave it as it is": the screen never receives the
        # current secret, so submitting the form would otherwise erase it.
        if not valeur and cle in {c.cle for f in CATALOGUE for c in f.champs if c.secret}:
            continue
        db.execute(
            "INSERT INTO integration_config (cle, valeur, categorie, maj_par, maj_le) VALUES (%s, %s, 'email', %s, now()) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
            (cle, valeur or None, user.id),
            role=user.role,
        )
        ecrits.append(cle)
    audit.log(user.id, user.role, "maj_email_fournisseur", "integration_config", contexte, {"cles": sorted(ecrits)})
    return ecrits


@router.put("/fournisseurs/{code}")
def enregistrer_fournisseur(
    code: str,
    payload: ValeursIn,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, object]:
    """Save one provider's fields in a single submit, then report its readiness."""
    f = _PAR_CODE.get(code)
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fournisseur inconnu")
    permises = {c.cle for c in f.champs} | {c.cle for c in _COMMUNS}
    hors = [c for c in payload.valeurs if c not in permises]
    if hors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"réglages étrangers à {f.libelle} : {', '.join(sorted(hors))}")
    _enregistrer(payload.valeurs, user, code)
    valeurs = _valeurs()
    return {"ok": True, "etat": _etat(f, valeurs, _chaine_active())}


@router.put("/commun")
def enregistrer_commun(
    payload: ValeursIn,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, object]:
    """Save the sender identity shared by every provider."""
    permises = {c.cle for c in _COMMUNS}
    hors = [c for c in payload.valeurs if c not in permises]
    if hors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"réglages étrangers à l'identité d'expéditeur : {', '.join(sorted(hors))}")
    _enregistrer(payload.valeurs, user, "commun")
    return {"ok": True}


class TestIn(BaseModel):
    destinataire: EmailStr | None = None


@router.post("/fournisseurs/{code}/test")
def tester(
    code: str,
    payload: TestIn,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, object]:
    """Send a real message through this provider alone, active or not.

    No fallback: the result must describe this provider, otherwise a broken one
    could be activated on the strength of another one's success.
    """
    from .email_gateway import send_via

    f = _PAR_CODE.get(code)
    if f is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="fournisseur inconnu")
    manque = _manquants(f, _valeurs(), _chaine_active())
    if manque:
        return {"envoye": False, "fournisseur": code, "erreur": f"Configuration incomplète : {', '.join(manque)}."}

    dest = str(payload.destinataire) if payload.destinataire else str(user.email)
    titre = f"Test d'envoi via {f.libelle}"
    corps = (
        f"Message de test envoyé par la plateforme via {f.libelle}. "
        "Le recevoir prouve que ce fournisseur peut acheminer les messages aux membres."
    )
    envoye, erreur = send_via(code, dest, titre, corps, f"<p>{corps}</p>")
    audit.log(user.id, user.role, "test_email_fournisseur", "integration_config", code, {"to": dest, "envoye": envoye})
    return {"envoye": envoye, "fournisseur": code, "destinataire": dest, "erreur": erreur}


class ChaineIn(BaseModel):
    #: Ordered: the first carries the traffic, the following ones catch a failure.
    chaine: list[str]


@router.put("/chaine")
def definir_chaine(
    payload: ChaineIn,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, object]:
    """Make a provider the active one, with optional fallbacks behind it.

    Refuses a chain whose providers are not configured. Letting an administrator
    activate an empty provider would turn one click into a silent outage of every
    connection code and every convocation.
    """
    demandee = [c.strip() for c in payload.chaine if c.strip()]
    if not demandee:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choisissez au moins un fournisseur.")
    inconnus = [c for c in demandee if c not in _PAR_CODE]
    if inconnus:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"fournisseur inconnu : {', '.join(inconnus)}")
    if len(set(demandee)) != len(demandee):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un fournisseur ne peut figurer deux fois dans la chaîne.")

    valeurs = _valeurs()
    for code in demandee:
        manque = _manquants(_PAR_CODE[code], valeurs, demandee)
        if manque:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{_PAR_CODE[code].libelle} n'est pas configuré : il manque {', '.join(manque)}. Renseignez-le et testez-le avant de l'activer.",
            )

    ancienne = _chaine_active()
    db.execute(
        "INSERT INTO integration_config (cle, valeur, categorie, maj_par, maj_le) VALUES (%s, %s, 'email', %s, now()) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
        (CLE_CHAINE, ",".join(demandee), user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "bascule_fournisseur_email", "integration_config", CLE_CHAINE, {"avant": ancienne, "apres": demandee})
    return {"ok": True, "chaine": demandee, "avant": ancienne}
