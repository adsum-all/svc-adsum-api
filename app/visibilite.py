"""Activity visibility predicate shared by the agenda and the action endpoints.

An event is visible to a member when it targets the whole community
(``cible_type = 'general'``) or when it targets an organisational unit the
member belongs to: their commission, their intendance, the coordination of that
intendance, or their tribu. The same rule gates both the agenda listing
(``my_events``) and every member action taken by event id, so an event hidden
from a member can never be acted upon by guessing its id.
"""
from __future__ import annotations

from . import db

# Reusable WHERE fragment. It expects the query to expose ``e`` (evenement),
# ``m`` (the member row) and ``mi`` (LEFT JOIN intendance on m.intendance_id).
CIBLE_PREDICATE = (
    "("
    "e.cible_type = 'general'"
    " OR (e.cible_type = 'commission' AND e.cible_id = m.commission_id)"
    " OR (e.cible_type = 'intendance' AND e.cible_id = m.intendance_id)"
    " OR (e.cible_type = 'coordination' AND e.cible_id = mi.coordination_id)"
    " OR (e.cible_type = 'tribu' AND e.cible_id = m.tribu_id)"
    ")"
)


def evenement_visible_membre(evenement_id: str, membre_id: str, role: str) -> bool:
    """Return whether ``membre_id`` may see and act on ``evenement_id``.

    Parameters
    ----------
    evenement_id : str
        The event identifier to check.
    membre_id : str
        The authenticated member.
    role : str
        The member's role, used for the database access scope.

    Returns
    -------
    bool
        ``True`` when the event is general or targeted at one of the member's
        organisational units, ``False`` otherwise (including an unknown event).
    """
    row = db.fetch_one(
        f"""
        SELECT 1
        FROM evenement e
        JOIN membre m ON m.id = %s
        LEFT JOIN intendance mi ON mi.id = m.intendance_id
        WHERE e.id = %s AND {CIBLE_PREDICATE}
        """,
        (membre_id, evenement_id),
        role=role,
    )
    return row is not None
