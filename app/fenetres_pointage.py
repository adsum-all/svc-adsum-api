"""How long an activity stays open for attendance, as the organisation decides it.

Two durations were literals repeated across four queries: attendance opened fifteen
minutes before an activity, and an activity with no stated end was treated as lasting
two hours. An organisation whose prayer runs forty minutes could not say so, and one
wanting a quarter of an hour of tolerance could not either. Worse, the four copies
could drift apart, so what the member saw and what the server accepted were two
separate decisions that only happened to agree.

One expression, read from settings, in minutes. Minutes because that is the unit that
expresses both a quarter of an hour and a whole day, and because the shortest window
anybody could previously set was one hour, which was exactly the limitation reported.

The values are SQL fragments rather than Python numbers on purpose: the window has to
be computed by the same query that filters on it, or a listing and its guard would
disagree again the moment one of them is edited.
"""
from __future__ import annotations

from . import db

CLE_AVANT = "pointage_ouverture_avant_minutes"
CLE_DUREE = "pointage_duree_defaut_minutes"

DEFAUT_AVANT = 15
DEFAUT_DUREE = 120

# An activity open for more than a fortnight is not an activity any more, and a
# negative window would silently invert the comparison it feeds.
MAXIMUM_MINUTES = 20160


def _minutes(cle: str, defaut: int, role: str | None = None) -> int:
    try:
        row = db.fetch_one(
            "SELECT (valeur #>> '{}')::int AS n FROM parametre WHERE cle = %s",
            (cle,), role=role,
        )
    except Exception:  # noqa: BLE001 - a settings read must never break a listing
        return defaut
    if not row or row.get("n") is None:
        return defaut
    return max(0, min(MAXIMUM_MINUTES, int(row["n"])))


def ouverture_avant(role: str | None = None) -> int:
    """Minutes before the start at which attendance opens."""
    return _minutes(CLE_AVANT, DEFAUT_AVANT, role)


def duree_defaut(role: str | None = None) -> int:
    """Minutes an activity lasts when it states no end time."""
    return _minutes(CLE_DUREE, DEFAUT_DUREE, role)


def fin_effective_sql(alias: str = "e", duree: int | None = None) -> str:
    """SQL for when an activity ends, its own end or the configured default.

    Takes the alias so it composes into any query, and the duration as a plain integer
    already read from settings, so a listing resolves the setting once instead of once
    per row.
    """
    minutes = DEFAUT_DUREE if duree is None else max(0, min(MAXIMUM_MINUTES, duree))
    return f"COALESCE({alias}.fin, {alias}.debut + make_interval(mins => {minutes}))"


def debut_fenetre_sql(alias: str = "e", avant: int | None = None) -> str:
    """SQL for when attendance opens, the start minus the configured tolerance."""
    minutes = DEFAUT_AVANT if avant is None else max(0, min(MAXIMUM_MINUTES, avant))
    return f"({alias}.debut - make_interval(mins => {minutes}))"


def cloture_declaration_sql(alias: str = "e") -> str:
    """SQL for the instant the declaration form closes.

    The activity ending and the form closing are two different moments: a member is
    given a window afterwards to say whether they followed it. Listings used to treat
    the activity's end as the end of everything, so during that window the activity
    belonged to no list at all. The member could not answer a form that was still
    open, and the missing answer was then counted as a non-response, which entered the
    statistics as if the member had chosen not to reply.

    The window is the activity's own value in minutes first, then the same value
    expressed in hours for activities configured before minutes existed, then the
    global setting. Kept here so display, submission and reminders cannot drift apart.
    """
    return (
        f"(COALESCE({alias}.fin, {alias}.debut + make_interval(mins => COALESCE("
        "  (SELECT (p.valeur #>> '{}')::int FROM parametre p WHERE p.cle = 'pointage_duree_defaut_minutes'), 120)))"
        " + make_interval(mins => COALESCE("
        f"{alias}.fenetre_reponse_minutes, "
        f"{alias}.fenetre_reponse_heures * 60, "
        "(SELECT (p.valeur #>> '{}')::int FROM parametre p WHERE p.cle = 'questionnaire_fenetre_minutes'), "
        "120)))"
    )
