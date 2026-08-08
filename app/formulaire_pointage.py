"""The attendance declaration form, seen and governed from the back office.

The form a member fills after an activity decides what the whole platform can then
count. Until now it existed only in the code of four applications and in the reason
catalogue, so an administrator could neither see what members were being asked nor
change the wording without a deployment. What cannot be seen cannot be governed, and
the statistics inherit whatever the form happens to ask.

Two things are served here.

**The structure**, described rather than duplicated: the sequence of questions, their
wording, and the rule attached to each one. It is derived from the same constants the
member endpoint enforces, so the preview cannot drift from what is actually asked.

**The reason catalogue**, which is genuinely editable. Reordering, renaming, requiring
a comment, and retiring a reason are ordinary business decisions.

Two guarantees that are not negotiable, both enforced in :mod:`participation`:

- ``partiel`` describes an online follow-up that was incomplete. It is never an
  absence, and it can never accompany ``presentiel``.
- A member never qualifies their own absence as excused. Only a habilitated person
  decides, from the pilotage tool, and the decision carries who and when.

A reason is retired, never deleted: absences already recorded cite it, and erasing the
row would leave those records pointing at nothing.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/formulaire-pointage", tags=["formulaire-pointage"])

_CODE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")


def _structure() -> list[dict[str, object]]:
    """The questions in the order a member meets them, with the rule behind each.

    Written once here and rendered by the back office, so the administrator reads the
    same sequence the member answers instead of a description maintained separately.
    """
    return [
        {
            "rang": 1,
            "question": "Avez-vous suivi cette activité ?",
            "reponses": ["Oui, j'ai suivi cette activité", "Non, je n'ai pas suivi cette activité"],
            "regle": (
                "La question porte sur le suivi, pas sur la présence physique. Un membre qui a suivi "
                "l'activité à distance répond oui."
            ),
            "modifiable": False,
        },
        {
            "rang": 2,
            "question": "Comment avez-vous suivi l'activité ?",
            "reponses": ["En présentiel", "En ligne"],
            "regle": (
                "Posée seulement après un oui, et seulement si le membre n'a pas été scanné. Un scan vaut "
                "présence physique prouvée : le membre ne peut alors ni choisir un autre mode, ni déclarer "
                "une absence."
            ),
            "modifiable": False,
        },
        {
            "rang": 3,
            "question": "Votre suivi en ligne était-il complet ou partiel ?",
            "reponses": ["J'ai suivi l'activité en entier", "Je n'ai suivi qu'une partie"],
            "regle": (
                "Posée uniquement pour un suivi en ligne. Partiel qualifie un suivi à distance incomplet : "
                "ce n'est jamais une absence, et cela ne peut pas accompagner le présentiel. Les statistiques "
                "comptent ce membre parmi ceux qui ont suivi."
            ),
            "modifiable": False,
        },
        {
            "rang": 4,
            "question": "Souhaitez-vous indiquer la raison de votre absence ?",
            "reponses": ["Choix dans le catalogue ci-dessous, la réponse reste facultative"],
            "regle": (
                "Posée après un non, et facultative. Le membre indique une raison, il ne qualifie jamais son absence "
                "d'excusée : seule une personne habilitée le décide depuis l'outil de pilotage, et la "
                "décision garde qui l'a prise et quand."
            ),
            "modifiable": True,
        },
    ]


def _catalogue(role: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "code": str(r["code"]),
            "libelle": str(r["libelle"]),
            "libelle_en": r["libelle_en"],
            "ordre": int(r["ordre"]),
            "actif": bool(r["actif"]),
            "commentaire_requis": bool(r["commentaire_requis"]),
            "utilisations": int(r["utilisations"]),
        }
        for r in db.fetch_all(
            "SELECT m.code, m.libelle, m.libelle_en, m.ordre, m.actif, m.commentaire_requis, "
            "  (SELECT count(*) FROM participation p WHERE p.absence_motif = m.code) AS utilisations "
            "FROM motif_absence m ORDER BY m.ordre, m.libelle",
            (),
            role=role,
        )
    ]


@router.get("")
def lire(user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))]) -> dict[str, object]:
    """The whole form as the member meets it, plus the editable catalogue."""
    return {
        "titre": "Déclaration de suivi d'activité",
        "introduction": (
            "Indiquez si vous avez suivi cette activité. Cette déclaration ne remplace pas un scan : "
            "si vous avez été scanné à l'entrée, votre présence est déjà enregistrée."
        ),
        "structure": _structure(),
        "catalogue": _catalogue(user.role),
    }


class MotifIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    libelle: str = Field(min_length=2, max_length=120)
    libelle_en: str = Field(default="", max_length=120)
    ordre: int = Field(default=50, ge=0, le=999)
    commentaire_requis: bool = False


def _valider_code(code: str) -> str:
    propre = code.strip().lower()
    if not _CODE.match(propre):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le code doit faire 3 à 40 caractères, en minuscules, sans accent ni espace (exemple : deplacement_professionnel).",
        )
    return propre


@router.post("/motifs", status_code=status.HTTP_201_CREATED)
def creer_motif(
    payload: MotifIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, object]:
    """Add a reason. The code is technical and permanent, the wording is not.

    Absences store the code, so it must never change afterwards: renaming the label
    updates every past absence's display, renaming the code would orphan them.
    """
    code = _valider_code(payload.code)
    if db.fetch_one("SELECT code FROM motif_absence WHERE code = %s", (code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Le code {code} existe déjà.")
    db.execute(
        "INSERT INTO motif_absence (code, libelle, libelle_en, ordre, actif, commentaire_requis) "
        "VALUES (%s, %s, %s, %s, true, %s)",
        (code, payload.libelle.strip(), payload.libelle_en.strip() or None, payload.ordre, payload.commentaire_requis),
        role=user.role,
    )
    audit.log(user.id, user.role, "creer_motif_absence", "motif_absence", code, {"libelle": payload.libelle.strip()})
    return {"ok": True, "catalogue": _catalogue(user.role)}


class MotifPatch(BaseModel):
    libelle: str | None = Field(default=None, min_length=2, max_length=120)
    libelle_en: str | None = Field(default=None, max_length=120)
    ordre: int | None = Field(default=None, ge=0, le=999)
    actif: bool | None = None
    commentaire_requis: bool | None = None


@router.patch("/motifs/{code}")
def modifier_motif(
    code: str,
    payload: MotifPatch,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, object]:
    """Change the wording, the order, or whether the reason is still offered.

    Retiring is what replaces deleting: the reason stops being proposed, and the
    absences that already cite it keep a label to display.
    """
    if not db.fetch_one("SELECT code FROM motif_absence WHERE code = %s", (code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Motif inconnu.")

    champs: list[str] = []
    valeurs: list[object] = []
    for nom, valeur in (
        ("libelle", payload.libelle.strip() if payload.libelle is not None else None),
        ("libelle_en", payload.libelle_en.strip() or None if payload.libelle_en is not None else None),
        ("ordre", payload.ordre),
        ("actif", payload.actif),
        ("commentaire_requis", payload.commentaire_requis),
    ):
        if valeur is not None or (nom == "libelle_en" and payload.libelle_en is not None):
            champs.append(f"{nom} = %s")
            valeurs.append(valeur)
    if not champs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune modification demandée.")

    # Retiring the last offered reason would leave a member unable to answer the
    # question at all, so the form would ask something it refuses to accept.
    if payload.actif is False:
        restants = db.fetch_one(
            "SELECT count(*) AS n FROM motif_absence WHERE actif AND code <> %s", (code,), role=user.role
        )
        if not restants or int(restants["n"]) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="C'est le dernier motif proposé. En le retirant, un membre ne pourrait plus justifier une absence.",
            )

    valeurs.append(code)
    db.execute(f"UPDATE motif_absence SET {', '.join(champs)} WHERE code = %s", tuple(valeurs), role=user.role)
    audit.log(user.id, user.role, "maj_motif_absence", "motif_absence", code, payload.model_dump(exclude_none=True))
    return {"ok": True, "catalogue": _catalogue(user.role)}


class OrdreIn(BaseModel):
    #: Codes in the order they must appear to a member.
    codes: list[str]


@router.put("/motifs/ordre")
def reordonner(
    payload: OrdreIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, object]:
    """Set the order in one call, so a drag never leaves the list half-renumbered."""
    connus = {str(r["code"]) for r in db.fetch_all("SELECT code FROM motif_absence", (), role=user.role)}
    inconnus = [c for c in payload.codes if c not in connus]
    if inconnus:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Motif inconnu : {', '.join(inconnus)}")
    if len(set(payload.codes)) != len(payload.codes):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Un motif figure deux fois dans l'ordre demandé.")
    for rang, code in enumerate(payload.codes, start=1):
        db.execute("UPDATE motif_absence SET ordre = %s WHERE code = %s", (rang * 10, code), role=user.role)
    audit.log(user.id, user.role, "reordonner_motifs_absence", "motif_absence", "ordre", {"codes": payload.codes})
    return {"ok": True, "catalogue": _catalogue(user.role)}
