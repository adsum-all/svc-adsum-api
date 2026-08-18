"""Envoi de courriel pour les services internes de l'éditeur.

Une porte étroite, ouverte au seul service commerce, pour qu'il n'ait pas à refaire
la chaîne d'envoi. ADSUM a déjà des fournisseurs configurés dans l'ordre, un
expéditeur validé chez chacun, une politique de repli et un journal. Une seconde
configuration ailleurs finirait par diverger, et le jour où l'adresse d'expédition
change, la moitié des messages partirait en indésirables sans que personne ne le voie.

Ce que cette route n'est pas : un moyen d'écrire à un membre. Elle n'accepte pas
d'identifiant de membre, ne consulte aucune table, et le destinataire est une adresse
que l'appelant fournit déjà. Elle ne donne donc accès à aucune donnée personnelle
qu'un service interne ne détiendrait pas.

L'authentification est celle des tâches planifiées : les deux services appartiennent
au même exploitant, et aucun utilisateur n'est derrière cet appel.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from .cron_auth import require_cron_auth
from .email_gateway import send_email


def _verifier_ordonnanceur(authorization: Annotated[str | None, Header()] = None) -> None:
    """L'authentification, posée en dépendance et non dans le corps du gestionnaire.

    L'ordre compte. Une vérification écrite dans le gestionnaire s'exécute après la
    validation du corps, donc un appelant sans jeton reçoit un 422 qui lui récite ce
    qu'il a envoyé et lui décrit le schéma attendu. En dépendance, elle passe avant :
    un inconnu obtient 401 et rien d'autre.
    """
    require_cron_auth(authorization)


router = APIRouter(
    prefix="/api/v1/interne", tags=["interne"],
    dependencies=[Depends(_verifier_ordonnanceur)],
)

#: Bornes de taille. Un objet démesuré est refusé par les fournisseurs, et un corps
#: de plusieurs mégaoctets fait échouer l'envoi après avoir occupé la connexion.
OBJET_MAX = 300
CORPS_MAX = 50_000


class DemandeCourriel(BaseModel):
    destinataire: EmailStr
    objet: str = Field(min_length=1, max_length=OBJET_MAX)
    texte: str = Field(min_length=1, max_length=CORPS_MAX)
    #: Qui demande l'envoi. Journalisé, jamais montré au destinataire : sert à
    #: retrouver la cause quand un message part en trop ou pas du tout.
    origine: str = Field(default="interne", max_length=40)
    #: Identifie le message pour que deux appels n'en produisent qu'un seul envoi.
    #: Un délai dépassé est indiscernable d'un échec : l'appelant ne sait pas si le
    #: message est parti, il le représente, et le destinataire reçoit deux fois le
    #: même avertissement. Facultative, parce qu'un appelant qui ne peut pas en
    #: fournir doit tout de même pouvoir écrire.
    cle_idempotence: str | None = Field(default=None, max_length=120)


@router.post("/courriel")
def envoyer_courriel(demande: DemandeCourriel) -> dict[str, object]:
    """Faire partir un message par la chaîne d'envoi de la plateforme.

    Renvoie l'issue plutôt que de lever sur un refus du fournisseur : l'appelant
    garde son message en boîte et le représentera, ce qui vaut mieux qu'une erreur
    qu'il interpréterait comme un envoi peut-être parti.
    """
    if demande.cle_idempotence:
        deja = _deja_envoye(demande.cle_idempotence)
        if deja is not None:
            # Réponse identique à celle d'un premier envoi : l'appelant marque son
            # message comme parti, ce qui est le cas.
            return {"envoye": True, "fournisseur": deja, "deja_envoye": True}

    envoye, fournisseur = send_email(
        to=str(demande.destinataire),
        subject=demande.objet,
        text=demande.texte,
        # Le même texte en HTML, échappé. Sans partie HTML, certains clients de
        # messagerie affichent le message comme une pièce jointe.
        html=_en_html(demande.texte),
    )
    if not envoye:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Aucun fournisseur n'a accepté le message (dernier : {fournisseur})",
        )
    if demande.cle_idempotence:
        _noter_envoi(demande.cle_idempotence, fournisseur)
    return {"envoye": True, "fournisseur": fournisseur}


def _en_html(texte: str) -> str:
    """Le texte, échappé et découpé en paragraphes.

    L'échappement n'est pas optionnel : le corps vient d'un autre service, et un nom
    d'organisation contenant un chevron produirait un message cassé au mieux, une
    injection au pire.
    """
    from html import escape

    paragraphes = [p.strip() for p in texte.split("\n\n") if p.strip()]
    corps = "".join(
        f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphes
    )
    return f'<div style="font-family:system-ui,sans-serif;font-size:15px">{corps}</div>'


#: Les envois déjà faits, gardés le temps qu'une reprise puisse arriver.
#:
#: En mémoire du processus et non en base, délibérément. Ce cache ne protège que du
#: doublon immédiat, celui d'un délai dépassé suivi d'une reprise dans la minute, et
#: c'est le seul cas fréquent. Le rendre durable demanderait une table, un index et
#: une purge, pour un gain qui ne se manifeste qu'après un redémarrage entre les deux
#: appels. La boîte d'envoi de l'appelant, elle, est bien en base et porte l'état
#: définitif : ce cache est un filet, pas la garantie.
_ENVOIS_RECENTS: dict[str, tuple[str, float]] = {}

#: Au-delà, une reprise n'est plus une reprise, c'est un nouvel envoi voulu.
RETENTION_S = 900

#: Plafond du cache. Sans lui, un appelant qui varie sa clé fait grossir la mémoire
#: du processus sans limite.
TAILLE_MAX = 5000


def _deja_envoye(cle: str) -> str | None:
    import time

    entree = _ENVOIS_RECENTS.get(cle)
    if entree is None:
        return None
    fournisseur, quand = entree
    if time.monotonic() - quand > RETENTION_S:
        _ENVOIS_RECENTS.pop(cle, None)
        return None
    return fournisseur


def _noter_envoi(cle: str, fournisseur: str) -> None:
    import time

    maintenant = time.monotonic()
    if len(_ENVOIS_RECENTS) >= TAILLE_MAX:
        # Purge des entrées expirées d'abord ; si tout est frais, on vide, parce que
        # perdre le filet vaut mieux que faire grossir la mémoire indéfiniment.
        expirees = [c for c, (_, q) in _ENVOIS_RECENTS.items()
                    if maintenant - q > RETENTION_S]
        for c in expirees:
            _ENVOIS_RECENTS.pop(c, None)
        if len(_ENVOIS_RECENTS) >= TAILLE_MAX:
            _ENVOIS_RECENTS.clear()
    _ENVOIS_RECENTS[cle] = (fournisseur, maintenant)
