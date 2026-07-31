"""Read and change what an organisation calls its own units and responsibilities.

The words appear on every screen, in every listing, in every message. Changing one
changes the whole application's language, which is exactly the point for a platform
meant to be handed to organisations that speak differently, and exactly why the change
is journalled and bounded.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe
from .vocabulaire import TERMES, cle, rendu

router = APIRouter(prefix="/api/v1/admin/vocabulaire", tags=["parametres"])

# Articles a French sentence can actually use before one of these words. Anything else
# would produce "de le tribu" somewhere in the interface.
ARTICLES = ("le", "la", "les", "l'")

# What each term names, so somebody renaming it knows what they are renaming.
_ROLE_DU_TERME = {
    "coordination": "Le plus grand ensemble organisationnel, qui regroupe les autres unités.",
    "intendance": "Une unité de terrain, rattachée à une coordination.",
    "commission": "Un groupe de travail thématique, transversal aux unités.",
    "tribu": "Un groupe d'appartenance des membres, distinct des unités de travail.",
    "berger": "La personne qui accompagne spirituellement un groupe de membres.",
    "patriarche": "La personne qui préside un groupe d'appartenance.",
    "membre": "La personne inscrite dans l'organisation.",
}


@router.get("")
def lire(
    user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))],
) -> dict[str, Any]:
    """Every term, what it names, and how this organisation says it."""
    mots = rendu(user.role)
    lignes = db.fetch_all(
        "SELECT cle, valeur FROM integration_config WHERE cle LIKE %s",
        ("org_mot_%",), role=user.role,
    )
    renseignes = {str(r["cle"]) for r in lignes if (r.get("valeur") or "").strip()}
    return {
        "items": [
            {
                "terme": terme,
                "role": _ROLE_DU_TERME.get(terme, ""),
                "singulier": mots[terme]["singulier"],
                "pluriel": mots[terme]["pluriel"],
                "article": mots[terme]["article"],
                "exemple": f"{mots[terme]['avec_article']}, {mots[terme]['pluriel']}",
                "defaut_singulier": TERMES[terme]["singulier"],
                "defaut_pluriel": TERMES[terme]["pluriel"],
                "par_defaut": not any(
                    cle(terme, f) in renseignes for f in ("singulier", "pluriel", "article")
                ),
            }
            for terme in TERMES
        ],
        "articles": list(ARTICLES),
    }


class MotIn(BaseModel):
    singulier: str = Field(min_length=1, max_length=40)
    pluriel: str = Field(min_length=1, max_length=40)
    article: str = Field(min_length=1, max_length=4)


@router.put("/{terme}")
def enregistrer(
    terme: str,
    payload: MotIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))],
) -> dict[str, Any]:
    """Rename one term. The three facets move together, or the sentences break."""
    if terme not in TERMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="terme inconnu")
    article = payload.article.strip().lower()
    if article not in ARTICLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"article attendu parmi : {', '.join(ARTICLES)}",
        )
    valeurs = {
        "singulier": payload.singulier.strip(),
        "pluriel": payload.pluriel.strip(),
        "article": article,
    }
    # Written in one transaction: a half-applied rename would leave the singular from
    # one vocabulary next to the plural of another, on the same screen.
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        for facette, valeur in valeurs.items():
            cur.execute(
                "INSERT INTO integration_config (cle, valeur, categorie) VALUES (%s, %s, 'organisation') "
                "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_le = now()",
                (cle(terme, facette), valeur),
            )
    audit.log(user.id, user.role, "maj_vocabulaire", "integration_config", terme, valeurs)
    return {"terme": terme, **valeurs}


@router.delete("/{terme}")
def reinitialiser(
    terme: str,
    user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))],
) -> dict[str, Any]:
    """Go back to the word the platform ships with."""
    if terme not in TERMES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="terme inconnu")
    db.execute(
        "UPDATE integration_config SET valeur = NULL, maj_le = now() WHERE cle = ANY(%s)",
        ([cle(terme, f) for f in ("singulier", "pluriel", "article")],), role=user.role,
    )
    audit.log(user.id, user.role, "reinitialisation_vocabulaire", "integration_config", terme, {})
    return {"terme": terme, **TERMES[terme]}
