"""Shared activity engine.

A real activity is a row in the ``evenement`` table: it feeds the member agenda,
attendance and questionnaires through the existing targeting rules. Several callers
create activities: the back office (full form in ``admin.py``), the pilotage layer
(a responsable, bounded to their perimeter) and the collaboration spaces (a card
published as an activity). They must all land the exact same row shape and honour
the same targeting validation, so that single engine lives here.

The engine deliberately never forces ``session_ouverte``: an activity is opened for
attendance separately, when its session actually starts. Broadcasting to everyone
(``general``) stays a caller-side decision; this module only validates that a
targeted activity names an existing unit.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from . import db

# Organisational units a targeted activity can address, mapped to their table.
CIBLE_UNITES = {
    "coordination": "coordination",
    "commission": "commission",
    "intendance": "intendance",
    "tribu": "tribu",
}

# Every target kind the simple engine accepts. ``general`` reaches all members;
# the unit kinds reach one organisational unit; refinements (gender, age, list)
# stay reserved to the full back-office form.
TYPES_CIBLE = ("general", *CIBLE_UNITES.keys())


def valider_cible(cible_type: str, cible_id: str | None, role: str | None) -> str | None:
    """Validate a simple activity target and return the unit id to store.

    Parameters
    ----------
    cible_type : str
        One of :data:`TYPES_CIBLE`.
    cible_id : str or None
        The organisational unit id, required for a unit target, ignored otherwise.
    role : str or None
        The caller role, used for the row-level security context of the lookup.

    Returns
    -------
    str or None
        The validated unit id, or ``None`` for a ``general`` target.

    Raises
    ------
    HTTPException
        400 if the kind is unknown, the id is missing for a unit target, or the
        named unit does not exist.
    """
    if cible_type not in TYPES_CIBLE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_type invalide")
    if cible_type == "general":
        return None
    if not cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id requis pour une activite ciblee")
    table = CIBLE_UNITES[cible_type]
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (cible_id,), role=role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unite ciblee introuvable")
    return cible_id


def inserer_evenement(
    *,
    titre: str,
    type_: str | None,
    debut: object,
    fin: object = None,
    lieu: str | None = None,
    mode: str | None = None,
    cible_type: str = "general",
    cible_id: str | None = None,
    visibilite: str = "membres",
    cree_par: str,
    role: str | None,
) -> str:
    """Insert one real activity and return its id.

    The target is validated with :func:`valider_cible`; ``session_ouverte`` is left
    to its schema default (closed) so a freshly created activity is never opened for
    attendance before its session starts.
    """
    stored_id = valider_cible(cible_type, cible_id, role)
    created = db.execute(
        "INSERT INTO evenement (titre, type, mode, debut, fin, lieu, cible_type, cible_id, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (titre, type_, mode, debut, fin, lieu, cible_type, stored_id, visibilite, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation activite impossible")
    return str(created["id"])
