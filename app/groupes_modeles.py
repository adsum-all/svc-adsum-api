"""Request and response models of the access-group endpoints.

Split out of ``groupes.py``, which had grown past the size the project allows. The
models carry the documentation an administrator reads before granting a group, so a
custom group can be described as precisely as a standard one.
"""
# ruff: noqa: E501
from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import BaseModel

from .fields import ShortStr


class MembreGroupeIn(BaseModel):
    groupe_id: ShortStr
    portee_type: str = "global"
    portee_id: str | None = None


class GroupeOut(BaseModel):
    id: str
    cle: str
    libelle: str
    description: str | None = None
    role_accorde: str
    mode: str = "role"
    permissions: list[str] = []
    membres_count: int = 0
    systeme: bool = False
    actif: bool = True
    # Optional application this group serves (application.code), so the governance UI
    # can organise groups per application instead of one mixed list.
    application_code: str | None = None


# Documentation an administrator reads before granting a group. Carried on create
# and update so a custom group is described as precisely as a standard one, instead
# of being the only kind left undocumented.
_SENSIBILITES = ("faible", "moyen", "eleve", "critique")


class DocumentationGroupe(BaseModel):
    finalite: str | None = None
    usage_recommande: str | None = None
    usage_deconseille: str | None = None
    portee_texte: str | None = None
    sensibilite: str | None = None
    avertissement_securite: str | None = None
    custom_scope: str | None = None


class GroupeIn(DocumentationGroupe):
    cle: ShortStr
    libelle: ShortStr
    description: str | None = None
    # 'role': the group grants a platform role; 'permissions': it grants a set of
    # atomic permissions (the account role stays 'membre'). Default keeps the
    # historical behaviour for callers that only send role_accorde.
    mode: str = "role"
    role_accorde: str | None = None
    permissions: list[str] = []
    application_code: str | None = None


class UpdateGroupeIn(DocumentationGroupe):
    libelle: ShortStr | None = None
    description: str | None = None
    actif: bool | None = None
    # For a 'permissions' group, replace the whole granted permission set.
    permissions: list[str] | None = None
    application_code: str | None = None


def champs_documentation(payload: DocumentationGroupe) -> dict[str, object]:
    """Documentation fields the caller actually sent, validated."""
    envoyes = payload.model_dump(exclude_unset=True)
    out: dict[str, object] = {}
    for k in ("finalite", "usage_recommande", "usage_deconseille", "portee_texte",
              "avertissement_securite", "custom_scope"):
        if k in envoyes:
            valeur = envoyes[k]
            out[k] = valeur.strip() if isinstance(valeur, str) and valeur.strip() else None
    if "sensibilite" in envoyes and envoyes["sensibilite"] is not None:
        if envoyes["sensibilite"] not in _SENSIBILITES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"sensibilité invalide, valeurs admises : {', '.join(_SENSIBILITES)}")
        out["sensibilite"] = envoyes["sensibilite"]
    return out
