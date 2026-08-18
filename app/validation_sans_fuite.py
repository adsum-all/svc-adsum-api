"""Les erreurs de validation, sans recracher ce qui a été envoyé.

FastAPI répond aux corps mal formés par un 422 qui contient un champ ``input`` :
la valeur exacte que l'appelant a soumise, renvoyée telle quelle. Sur la plupart des
routes c'est commode. Sur ``/auth/login`` c'est le mot de passe en clair, dans le
corps de la réponse, et donc dans tout ce qui enregistre les réponses en chemin :
journaux du serveur, traces d'un intermédiaire, outil de suivi d'erreurs du
navigateur.

Le défaut a été constaté : une tentative de connexion mal formée a fait apparaître un
mot de passe réel dans une sortie d'outillage. Ce module le ferme pour toute l'API
plutôt que route par route, parce qu'une règle écrite endpoint par endpoint est une
règle oubliée sur le prochain endpoint.

Ce qui est conservé : où l'erreur se trouve et pourquoi. C'est ce dont un
développeur a besoin. La valeur soumise, il l'a déjà.
"""
from __future__ import annotations

from typing import Any

#: Les noms de champ dont la valeur ne sort jamais. Comparés en minuscules et par
#: fragment : « nouveau_mdp », « mdp_temporaire » et « password_confirm » sont tous
#: couverts par « mdp » et « password ».
FRAGMENTS_SENSIBLES = (
    "password", "mdp", "mot_de_passe", "motdepasse", "secret", "token",
    "jeton", "otp", "cle", "key", "signature", "authorization", "pin",
    "seed", "empreinte", "hash",
)

#: Ce qui remplace la valeur. Un marqueur explicite plutôt qu'une absence : un champ
#: disparu laisse croire qu'il n'a pas été envoyé, ce qui envoie chercher le défaut
#: au mauvais endroit.
MASQUE = "[valeur masquée]"


def champ_sensible(chemin: Any) -> bool:
    """Vrai si l'un des segments du chemin nomme un champ à ne pas divulguer."""
    if not isinstance(chemin, (list, tuple)):
        return False
    for segment in chemin:
        texte = str(segment).lower()
        if any(fragment in texte for fragment in FRAGMENTS_SENSIBLES):
            return True
    return False


def masquer_en_profondeur(valeur: Any, profondeur: int = 0) -> Any:
    """Masquer les clés sensibles à l'intérieur d'une valeur soumise.

    Indispensable, et pas seulement prudent. Une erreur « champ requis » porte en
    ``input`` non pas le champ fautif mais **le corps entier** de la requête. Un
    appel de connexion sans adresse produit donc une erreur située sur ``email``,
    donc non sensible au sens du chemin, dont la valeur contient le mot de passe.
    Masquer d'après l'emplacement seul laisse cette porte ouverte ; il faut regarder
    à l'intérieur.
    """
    if profondeur > 6:
        # Une structure profonde est tronquée plutôt que parcourue indéfiniment :
        # un corps replié sur lui-même ferait tourner le gestionnaire d'erreur.
        return MASQUE
    if isinstance(valeur, dict):
        return {
            cle: (MASQUE if _cle_sensible(str(cle))
                  else masquer_en_profondeur(sous_valeur, profondeur + 1))
            for cle, sous_valeur in valeur.items()
        }
    if isinstance(valeur, (list, tuple)):
        return [masquer_en_profondeur(element, profondeur + 1) for element in valeur]
    return _borner(valeur)


def _cle_sensible(nom: str) -> bool:
    minuscule = nom.lower()
    return any(fragment in minuscule for fragment in FRAGMENTS_SENSIBLES)


def nettoyer(erreurs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retirer les valeurs soumises des erreurs de validation.

    Deux passes, parce qu'il y a deux façons de fuir. Le champ fautif lui-même peut
    être sensible ; et la valeur soumise peut contenir un champ sensible même quand
    le champ fautif ne l'est pas.
    """
    propres: list[dict[str, Any]] = []
    for erreur in erreurs:
        copie = dict(erreur)
        if champ_sensible(copie.get("loc")):
            copie["input"] = MASQUE
            # Le contexte porte parfois la valeur une seconde fois, sous une autre
            # clé. Le retirer entièrement est plus sûr que de deviner laquelle.
            copie.pop("ctx", None)
        elif "input" in copie:
            copie["input"] = masquer_en_profondeur(copie["input"])
        propres.append(copie)
    return propres


def _borner(valeur: Any, limite: int = 200) -> Any:
    if isinstance(valeur, str) and len(valeur) > limite:
        return valeur[:limite] + "..."
    return valeur


def installer(app: Any) -> None:
    """Poser le gestionnaire sur l'application.

    Le code de réponse et la forme du corps restent ceux de FastAPI : un client qui
    lisait déjà ``detail`` continue de fonctionner.
    """
    from fastapi import Request, status
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def _validation(_requete: Request, erreur: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": nettoyer(list(erreur.errors()))},
        )
