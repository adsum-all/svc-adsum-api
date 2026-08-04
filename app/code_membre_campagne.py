"""Ask the members who never gave their member code to give it.

The code is issued by the organisation and normally everyone has one, but the form
only started asking for it properly after a great many members had already been
validated. Their record carries an empty column, their file is closed, and the
platform locks a validated member's fields: there is no way for them to supply the
code on their own, and no way for the organisation to ask.

This opens that door, for all of them at once. Each member gets what the
administration would have opened by hand: a request that says what is expected, the
member code unlocked on their record, a response window, and a notification. The
member fills it in from their own space and submits, which puts the value through
the ordinary review rather than writing it straight into the base.

Two things it deliberately does not do.

It never touches a member who already has a modification cycle open. Unlocking a
second set of fields inside somebody else's cycle would let them submit a change
nobody asked them for, and would consume the cycle the administration opened for a
different reason.

And it never assumes. A member who has answered that they hold no code is left
alone: they will be served when they obtain one, not chased for something they said
they do not have.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["membres"])

#: What the member sees as the subject of the request opened for them.
_SUJET = "Communiquez votre code membre"
#: The single field this campaign unlocks. Named explicitly rather than taken from a
#: caller: a bulk unlock that can open arbitrary fields is a bulk unlock nobody can
#: safely run twice.
_CHAMP = "code_membre"


class ResultatCampagne(BaseModel):
    """What the run did, or would have done when only simulating."""

    simulation: bool
    #: Validated members with no code and no declaration that they have none.
    eligibles: int
    #: Left alone because a modification cycle was already open for them.
    cycle_deja_ouvert: int
    #: Requests opened, fields unlocked, members notified.
    ouverts: int
    delai_jours: int


def _eligibles(role: str | None) -> list[dict[str, Any]]:
    """Validated members with no code, who have not said they have none.

    A member with an open cycle is returned too, and skipped later, so the report can
    say how many were left alone rather than silently counting fewer than there are.
    """
    return [
        dict(r) for r in db.fetch_all(
            "SELECT m.id, m.prenoms, "
            "  EXISTS (SELECT 1 FROM demande d WHERE d.membre_id = m.id "
            "          AND d.statut IN ('attente_membre', 'en_validation')) AS cycle_ouvert "
            "FROM membre m "
            "WHERE m.statut_inscription = 'approuve' "
            "  AND m.statut = 'actif' "
            "  AND (m.code_membre IS NULL OR btrim(m.code_membre) = '') "
            "  AND m.a_code_membre IS DISTINCT FROM false "
            "ORDER BY m.nom, m.prenoms",
            (), role=role,
        )
    ]


def _ouvrir_pour(membre_id: str, delai: int, role: str | None) -> bool:
    """Open one member's cycle: request, unlock, window, message. True when done."""
    # Imported here rather than at module scope: this keeps the dependency one-way
    # at import time, and reuses the very helpers the administration's own unlock
    # uses, so a member sees the same thread and the same notification either way.
    from .demandes import _notify_ticket, _system_message

    cree = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, champ_concerne, categorie, statut, echeance_reponse, maj_le) "
        "VALUES (%s, 'modification_info', %s, %s, 'modification_info', 'attente_membre', "
        "        now() + make_interval(days => %s), now()) RETURNING id",
        (membre_id, _SUJET, _CHAMP, delai), role=role,
    )
    if not cree:
        return False
    db.execute(
        "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
        ([_CHAMP], membre_id), role=role,
    )
    corps = (
        "Votre code membre n'est pas encore enregistré. Il vous est propre, il est "
        "délivré par l'organisation, et il est distinct du matricule que la plateforme "
        "vous a attribué. Le champ vient d'être ouvert dans votre espace : renseignez-le "
        "puis validez. Si vous n'en avez pas encore, laissez-le vide et dites-le nous : "
        "nous rouvrirons ce champ le jour où il vous sera remis."
    )
    _system_message(str(cree["id"]), role, corps)
    _notify_ticket(str(cree["id"]), role, "Votre code membre est attendu", corps)
    return True


@router.post("/membres/campagne-code-membre", response_model=ResultatCampagne)
def campagne_code_membre(
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
    simulation: bool = Query(default=True, description="Ne rien écrire, seulement compter."),
    delai_jours: int = Query(default=0, ge=0, le=365, description="Fenêtre de réponse; 0 pour le délai configuré."),
) -> ResultatCampagne:
    """Open a member-code request for every validated member who still has none.

    Simulating by default is not caution for its own sake: this writes to as many
    records as there are members concerned and notifies each one, so the count is
    worth reading before the notifications go out. Pass simulation=false to run it.

    Safe to run again. A member served by an earlier run has an open cycle and is
    skipped by the next, so repeating the call chases nobody twice.
    """
    from .demandes import _delai_deblocage_defaut

    delai = delai_jours or _delai_deblocage_defaut(user.role)
    membres = _eligibles(user.role)
    deja = [m for m in membres if m.get("cycle_ouvert")]
    a_ouvrir = [m for m in membres if not m.get("cycle_ouvert")]

    ouverts = 0
    if not simulation:
        for m in a_ouvrir:
            if _ouvrir_pour(str(m["id"]), delai, user.role):
                ouverts += 1
        audit.log(
            user.id, user.role, "campagne_code_membre", "membre", None,
            {"ouverts": ouverts, "ignores_cycle_ouvert": len(deja), "delai_jours": delai},
        )

    return ResultatCampagne(
        simulation=simulation,
        eligibles=len(membres),
        cycle_deja_ouvert=len(deja),
        ouverts=ouverts,
        delai_jours=delai,
    )
