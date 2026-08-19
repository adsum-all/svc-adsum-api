"""La frontière entre le monde client et le monde éditeur.

Ce module existe à cause d'un défaut précis. Les vingt routes de la console de
l'éditeur étaient gardées par ``require_permission("support.traiter")``. Cette
permission est accordée d'office aux rôles ``admin`` et ``super_admin``, qui sont des
rôles d'organisation cliente. Le trésorier d'une paroisse, administrateur dans SA
base, présentait un jeton disant « admin » et obtenait la liste de toutes les
organisations clientes d'ADSUM, avec le pouvoir d'en créer une, d'en suspendre une
autre et de modifier ses modules.

Aucune garde ne manquait : chacune de ces routes en avait une. Ce qui manquait, c'est
qu'un rôle ne peut pas porter cette frontière, parce que le même mot existe des deux
côtés. Trois barrières la portent désormais, et il faut les franchir toutes les trois.

1. **L'audience du jeton.** Un jeton client porte ``adsum-client``, un jeton éditeur
   porte ``adsum-editeur``. Ils ne sont pas interchangeables.

2. **Une signature distincte.** Le jeton éditeur est signé avec un secret propre. Même
   si le secret des tenants fuitait, il ne permettrait pas de fabriquer une autorité
   éditeur. C'est la barrière qui tient quand les deux autres ont cédé.

3. **Un registre d'opérateurs.** Être opérateur, c'est figurer dans ce registre, et
   rien d'autre. Le rôle porté par le jeton n'y change rien.

Et une quatrième, au-dessus : la capacité exercée doit être accordée au rôle éditeur
de la personne par la politique ``editor-access-policy.json``, qui est le document
faisant foi. La politique n'est pas une annexe documentaire : elle est lue ici, à
l'exécution, et une capacité qu'elle n'accorde pas est refusée.

Ce que ce module ne fait PAS encore, et qui est écrit noir sur blanc pour que personne
ne s'y trompe : le registre est aujourd'hui une variable de configuration, pas la
table ``operateur_editeur`` du schéma de l'éditeur. Cette table existe dans le service
commerce mais son schéma n'est pas encore posé en production. La configuration est un
relais volontairement étroit et fermé par défaut, pas une solution.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import settings
from .schemas import UserMe

#: L'audience d'un jeton d'organisation cliente. Absente des jetons émis avant la pose
#: de cette frontière : ceux-là sont acceptés côté client pendant la transition, jamais
#: côté éditeur. Déconnecter tout le monde pour poser une barrière qui protège l'autre
#: côté aurait été un dégât gratuit.
AUDIENCE_CLIENT = "adsum-client"

#: L'audience d'un jeton d'opérateur de l'éditeur. Exigée, sans exception ni transition.
AUDIENCE_EDITEUR = "adsum-editeur"

#: Durée d'une session interne. Bien plus courte qu'une session cliente : un compte
#: interne compromis n'expose pas une organisation mais le parc entier, et la fenêtre
#: pendant laquelle il reste exploitable est la seule variable qu'on maîtrise.
MINUTES_SESSION_EDITEUR = 240

_bearer = HTTPBearer(auto_error=True)


class Operateur(BaseModel):
    """Un employé de l'éditeur, tel que le jeton et le registre le décrivent."""

    utilisateur_id: str
    email: str
    role_editeur: str
    capacites: frozenset[str]
    #: Le rôle du compte dans l'annuaire, qui pilote la variable de session adsum.role
    #: et donc les politiques RLS. Il vient du jeton, émis après authentification, et
    #: non d'une revendication que l'appelant pourrait choisir : le jeton éditeur est
    #: signé avec un secret que le monde client n'a pas.
    role_bdd: str

    model_config = {"arbitrary_types_allowed": True}

    @property
    def id(self) -> str:
        """Alias lu par le journal d'audit et par les écritures de la console.

        Quatorze appels écrivent ``user.id`` comme acteur d'une mutation. Le nom vient
        de ``UserMe``, que ces routes recevaient avant la pose de la frontière. Le
        garder ici évite de toucher quatorze lignes d'audit pour un changement qui
        porte sur la garde, et surtout évite l'oubli d'une seule d'entre elles, qui
        aurait fait tomber une écriture en production sans que rien ne l'annonce.
        """
        return self.utilisateur_id

    @property
    def role(self) -> str:
        """Alias lu par la couche base. Les routes de console passent role=user.role
        depuis toujours ; garder ce nom évite de toucher vingt corps de fonction pour
        un changement qui porte sur la garde, pas sur les requêtes."""
        return self.role_bdd


class FrontiereFermee(HTTPException):
    """Refus de franchissement.

    Toujours 403 et jamais 404 : contrairement à une ressource d'un autre tenant, dont
    l'existence ne doit pas être confirmée, l'existence de la console de l'éditeur
    n'est pas un secret. Ce qui est refusé, c'est d'y entrer, et le dire clairement
    évite qu'on cherche la panne du mauvais côté.
    """

    def __init__(self, motif: str) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=motif)


# -- La politique, lue à l'exécution ------------------------------------------

def _chemin_politique() -> Path:
    """Où trouver la politique éditeur.

    La variable d'environnement l'emporte, ce qui permet à la CI de pointer sur
    l'exemplaire faisant foi fraîchement cloné. À défaut, la copie versionnée du
    dépôt, dont la tâche policy:drift garantit qu'elle n'a pas dérivé.
    """
    depuis_env = os.environ.get("ADSUM_POLICIES", "").strip()
    if depuis_env:
        return Path(depuis_env) / "editor-access-policy.json"
    return Path(__file__).resolve().parents[1] / "policies" / "editor-access-policy.json"


@lru_cache(maxsize=1)
def politique_editeur() -> dict[str, Any]:
    """La politique, chargée une fois.

    Une absence de fichier n'est pas rattrapée par un défaut permissif : sans
    politique, aucune capacité n'est accordée et toute la console répond 403. Un
    déploiement incomplet doit se voir, pas s'ouvrir.
    """
    chemin = _chemin_politique()
    if not chemin.exists():
        return {}
    with open(chemin, encoding="utf-8") as fichier:
        return json.load(fichier)


@lru_cache(maxsize=1)
def capacites_par_role() -> dict[str, frozenset[str]]:
    """Ce que chaque rôle éditeur peut exercer, héritage compris."""
    politique = politique_editeur()
    declares = {r["id"]: r for r in politique.get("roles", [])}

    def resoudre(identifiant: str, vus: frozenset[str] = frozenset()) -> set[str]:
        if identifiant in vus or identifiant not in declares:
            return set()
        role = declares[identifiant]
        acquis = set(role.get("grants", []))
        for parent in role.get("inherits", []):
            acquis |= resoudre(parent, vus | {identifiant})
        return acquis

    return {identifiant: frozenset(resoudre(identifiant)) for identifiant in declares}


# -- Le registre des opérateurs ------------------------------------------------

def registre_operateurs() -> dict[str, str]:
    """Qui est opérateur, et avec quel rôle éditeur.

    Format de ``ADSUM_OPERATEURS_EDITEUR`` : ``uuid:role,uuid:role``. Lu à chaque appel
    et non mis en cache : une révocation doit prendre effet à la requête suivante, et
    un cache d'un quart d'heure sur cette table est un quart d'heure d'accès offert à
    quelqu'un qu'on vient de renvoyer.

    Vide par défaut. Une plateforme sans opérateur déclaré n'ouvre la console à
    personne, ce qui est le bon comportement d'un déploiement qu'on n'a pas fini de
    configurer.
    """
    brut = os.environ.get("ADSUM_OPERATEURS_EDITEUR", "").strip()
    if not brut:
        return {}
    connus = capacites_par_role()
    registre: dict[str, str] = {}
    for entree in brut.split(","):
        identifiant, _, role = entree.strip().partition(":")
        identifiant, role = identifiant.strip(), role.strip()
        # Un rôle inconnu de la politique n'accorde rien. Le sauter plutôt que le
        # retenir évite qu'une faute de frappe dans une variable d'environnement
        # produise un opérateur sans capacité mais réputé légitime.
        if identifiant and role in connus:
            registre[identifiant] = role
    return registre


# -- Le jeton éditeur ----------------------------------------------------------

def _secret_editeur() -> str:
    """Le secret propre aux jetons éditeur.

    Distinct de celui des tenants, pour que la compromission de l'un ne fabrique pas
    l'autorité de l'autre. Vide signifie que la connexion éditeur est fermée : c'est
    un refus, pas un repli sur le secret des tenants.
    """
    return os.environ.get("ADSUM_JWT_EDITEUR_SECRET", "").strip()


def creer_jeton_editeur(utilisateur_id: str, email: str, role_bdd: str,
                        sid: str | None = None) -> str:
    """Émettre un jeton d'opérateur, si et seulement si la personne en est un."""
    secret = _secret_editeur()
    if not secret:
        raise FrontiereFermee(
            "La connexion éditeur n'est pas configurée sur cet environnement.")
    role = registre_operateurs().get(utilisateur_id)
    if not role:
        # Le message ne distingue pas « pas opérateur » de « registre vide » : dire
        # laquelle des deux renseignerait sur la configuration de la plateforme.
        raise FrontiereFermee("Ce compte n'est pas opérateur de l'éditeur.")
    maintenant = datetime.now(UTC)
    charge: dict[str, Any] = {
        "sub": utilisateur_id,
        "email": email,
        "role_editeur": role,
        "role_bdd": role_bdd,
        "aud": AUDIENCE_EDITEUR,
        "iss": "adsum-api",
        "iat": int(maintenant.timestamp()),
        "exp": int((maintenant + timedelta(minutes=MINUTES_SESSION_EDITEUR)).timestamp()),
    }
    if sid:
        charge["sid"] = sid
    return jwt.encode(charge, secret, algorithm=settings.jwt_algorithm)


def _decoder_jeton_editeur(jeton: str) -> dict[str, Any]:
    secret = _secret_editeur()
    if not secret:
        raise FrontiereFermee(
            "La connexion éditeur n'est pas configurée sur cet environnement.")
    return jwt.decode(
        jeton, secret, algorithms=[settings.jwt_algorithm],
        audience=AUDIENCE_EDITEUR, issuer="adsum-api",
        options={"require": ["sub", "aud", "exp", "role_editeur", "role_bdd"]},
    )


# -- Les dépendances FastAPI ---------------------------------------------------

def operateur_courant(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> Operateur:
    """L'opérateur derrière la requête, ou un refus.

    Trois vérifications, dans l'ordre où elles coûtent le moins cher : la signature et
    l'audience d'abord, qui ne touchent rien ; le registre ensuite, qui décide de la
    révocation ; la politique enfin, qui décide de l'étendue.
    """
    try:
        claims = _decoder_jeton_editeur(creds.credentials)
    except FrontiereFermee:
        raise
    except jwt.PyJWTError as exc:
        # Un jeton client présenté ici tombe exactement ici : mauvaise signature,
        # mauvaise audience, ou les deux. C'est la barrière la plus importante du
        # module, et elle ne consulte aucune base pour se prononcer.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton éditeur invalide.") from exc

    utilisateur_id = str(claims["sub"])
    role_actuel = registre_operateurs().get(utilisateur_id)
    if not role_actuel:
        # Le jeton est valide mais la personne n'est plus opératrice. La révocation
        # prime sur le jeton, sans quoi un renvoi ne prendrait effet qu'à l'expiration.
        raise FrontiereFermee("Cet opérateur a été révoqué.")
    # Le rôle retenu est celui du registre, jamais celui du jeton. Un rôle réduit
    # depuis l'émission doit s'appliquer immédiatement, sans attendre l'expiration.
    return Operateur(
        utilisateur_id=utilisateur_id,
        email=str(claims.get("email", "")),
        role_editeur=role_actuel,
        capacites=capacites_par_role().get(role_actuel, frozenset()),
        role_bdd=str(claims["role_bdd"]),
    )


def require_capacite(capacite: str):
    """Dépendance exigeant une capacité éditeur nommée.

    La capacité est écrite en toutes lettres à l'appel, et la politique dit qui la
    détient. Le test de conformité compare les deux listes : une route dont la
    capacité n'est pas déclarée, ou déclarée du mauvais côté, fait échouer la suite.
    """
    if not capacite.startswith("editor."):
        raise ValueError(
            f"« {capacite} » n'est pas une capacité éditeur. Le préfixe n'est pas "
            "décoratif : il empêche qu'une capacité cliente garde une route éditeur.")

    def dependance(
        operateur: Annotated[Operateur, Depends(operateur_courant)],
    ) -> Operateur:
        if capacite not in operateur.capacites:
            raise FrontiereFermee(
                f"Le rôle « {operateur.role_editeur} » n'a pas la capacité "
                f"« {capacite} ».")
        return operateur

    # La capacité est accrochée à la dépendance pour que le test de conformité puisse
    # la relire depuis la table de routage. Sans ce marqueur, il faudrait deviner la
    # garde d'une route en inspectant une fermeture, ce qui casserait au premier
    # remaniement et donnerait une conformité qui ne prouve rien.
    dependance.capacite_exigee = capacite  # type: ignore[attr-defined]
    return dependance


# -- L'échange de session ------------------------------------------------------

router = APIRouter(prefix="/api/v1/auth", tags=["frontiere"])


def _utilisateur_authentifie(creds: Annotated[HTTPAuthorizationCredentials,
                                              Depends(_bearer)]) -> UserMe:
    """La dépendance d'authentification cliente, résolue tardivement.

    Importée dans le corps et non en tête : ``auth`` et ce module se citent
    mutuellement, et l'import différé est la façon la plus simple de le dire sans
    déplacer une moitié de l'authentification ici.
    """
    from .auth import current_user

    return current_user(creds)


class SessionEditeur(BaseModel):
    """Le jeton d'opérateur, et ce qu'il ouvre."""

    access_token: str
    token_type: str = "bearer"
    role_editeur: str
    capacites: list[str]
    expire_dans_minutes: int


@router.post("/session-editeur", response_model=SessionEditeur)
def session_editeur(
    user: Annotated[UserMe, Depends(_utilisateur_authentifie)],
) -> SessionEditeur:
    """Échanger une session cliente authentifiée contre un jeton d'opérateur.

    Deux étapes distinctes, et c'est volontaire. L'authentification appartient à
    l'annuaire : mot de passe, second facteur, appareil de confiance, tout le chemin
    habituel s'applique, et rien n'est allégé pour les employés de l'éditeur, au
    contraire. L'autorisation appartient au registre des opérateurs, que cet échange
    consulte ; y figurer est la seule façon d'obtenir un jeton d'audience éditeur.

    Le jeton rendu est signé avec un secret distinct de celui des tenants. C'est ce
    qui fait qu'une fuite du secret des tenants ne fabrique aucune autorité éditeur :
    la séparation ne tient pas seulement à un champ dans la charge utile, elle tient
    à la clé.
    """
    jeton = creer_jeton_editeur(user.id, user.email, user.role, user.session_id)
    role = registre_operateurs()[user.id]
    return SessionEditeur(
        access_token=jeton,
        role_editeur=role,
        capacites=sorted(capacites_par_role().get(role, ())),
        expire_dans_minutes=MINUTES_SESSION_EDITEUR,
    )
