"""The durations an organisation decides for itself, gathered in one place.

Three judgements were made in the code and could not be revisited by the people who
live with them: how long a session survives without activity, how early attendance
opens before an activity, and how long an activity lasts when it states no end.

None of them is a technical fact. A parish sharing one computer in the sacristy wants
a short session; an organisation where everybody has their own laptop does not. A
prayer running forty minutes and a retreat running all day are both activities. This
platform is meant to serve organisations that have not been met yet, so their habits
cannot be written into it.

Everything is expressed in MINUTES, with the ready-made choices the interface offers
so nobody has to reason in raw numbers, and bounds that refuse a value which would
fight ordinary use rather than accepting it and being regretted later.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db, fenetres_pointage, session_inactivite
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/reglages/durees", tags=["parametres"])

# Each duration: what it governs, its bounds, and the choices worth offering. The
# suggestions are a convenience, never a restriction: any value inside the bounds is
# accepted, because an organisation may well want fifty minutes.
_DUREES: dict[str, dict[str, Any]] = {
    session_inactivite.CLE_PARAMETRE: {
        "libelle": "Fermeture de session sans activité",
        "aide": (
            "Au bout de combien de temps sans aucune action une session est fermée. "
            "La personne revient alors sur l'écran de connexion, et n'a pas à ressaisir "
            "de code si elle a accordé sa confiance à l'appareil."
        ),
        "defaut": session_inactivite.DEFAUT_MINUTES,
        "minimum": session_inactivite.MINIMUM_MINUTES,
        "maximum": session_inactivite.MAXIMUM_MINUTES,
        "zero_signifie": "jamais de fermeture automatique",
        "suggestions": [15, 30, 45, 60, 90, 120, 180, 240, 480, 720, 1440],
    },
    fenetres_pointage.CLE_AVANT: {
        "libelle": "Ouverture du pointage avant le début",
        "aide": (
            "Combien de temps avant le début d'une activité le pointage devient possible, "
            "pour que les personnes en avance puissent déjà se signaler."
        ),
        "defaut": fenetres_pointage.DEFAUT_AVANT,
        "minimum": 0,
        "maximum": 1440,
        "zero_signifie": "pointage ouvert à l'heure pile",
        "suggestions": [0, 5, 10, 15, 20, 30, 45, 60, 90, 120],
    },
    fenetres_pointage.CLE_DUREE: {
        "libelle": "Durée d'une activité sans heure de fin",
        "aide": (
            "Combien de temps une activité reste ouverte au pointage quand aucune heure "
            "de fin n'a été renseignée. Une activité qui porte sa propre heure de fin "
            "n'est pas concernée."
        ),
        "defaut": fenetres_pointage.DEFAUT_DUREE,
        "minimum": 5,
        "maximum": 1440,
        "zero_signifie": None,
        "suggestions": [15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 360, 480],
    },
}


def _lisible(minutes: int) -> str:
    """A duration said the way somebody would say it out loud."""
    if minutes <= 0:
        return "désactivé"
    if minutes < 60:
        return f"{minutes} minutes"
    heures, reste = divmod(minutes, 60)
    mot = "heure" if heures == 1 else "heures"
    if reste == 0:
        return f"{heures} {mot}"
    return f"{heures} {mot} {reste}"


@router.get("")
def lire(
    user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))],
) -> dict[str, Any]:
    """The three durations, their current value and what each one governs."""
    lignes = db.fetch_all(
        "SELECT cle, (valeur #>> '{}') AS valeur FROM parametre WHERE cle = ANY(%s)",
        (list(_DUREES),), role=user.role,
    )
    actuelles = {str(r["cle"]): r.get("valeur") for r in lignes}
    items = []
    for cle, meta in _DUREES.items():
        brut = actuelles.get(cle)
        valeur = int(brut) if brut not in (None, "") else int(meta["defaut"])
        items.append({
            "cle": cle,
            "libelle": meta["libelle"],
            "aide": meta["aide"],
            "minutes": valeur,
            "lisible": _lisible(valeur),
            "defaut": meta["defaut"],
            "minimum": meta["minimum"],
            "maximum": meta["maximum"],
            "zero_signifie": meta["zero_signifie"],
            "suggestions": [
                {"minutes": m, "lisible": _lisible(m)} for m in meta["suggestions"]
            ],
            "par_defaut": brut in (None, ""),
        })
    return {"items": items}


class DureeIn(BaseModel):
    minutes: int = Field(ge=0, le=20160)


@router.put("/{cle}")
def enregistrer(
    cle: str,
    payload: DureeIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))],
) -> dict[str, Any]:
    """Set one duration. Refused outside its bounds rather than quietly clamped.

    Clamping would leave the interface showing a number the organisation did not
    choose, and nothing saying why. A refusal names the bound.
    """
    meta = _DUREES.get(cle)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="réglage inconnu")
    minutes = int(payload.minutes)
    autorise_zero = meta["zero_signifie"] is not None
    if minutes == 0 and not autorise_zero:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"« {meta['libelle']} » ne peut pas être nul : minimum {meta['minimum']} minutes",
        )
    if minutes != 0 and (minutes < meta["minimum"] or minutes > meta["maximum"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"« {meta['libelle']} » attend une valeur entre {meta['minimum']} et "
                f"{meta['maximum']} minutes"
            ),
        )
    db.execute(
        "INSERT INTO parametre (cle, valeur) VALUES (%s, %s::jsonb) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur",
        (cle, json.dumps(minutes)), role=user.role,
    )
    audit.log(user.id, user.role, "maj_reglage_duree", "parametre", cle, {"minutes": minutes})
    return {"cle": cle, "minutes": minutes, "lisible": _lisible(minutes)}
