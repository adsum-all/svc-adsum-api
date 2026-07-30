"""Close a session that has been left alone, and say so in a way a client can act on.

A token lasted its full natural life whatever happened in between: an administrator
who walked away from an open back office on a shared machine left a working session
behind them for as long as the token was valid. Nothing closed it, because nothing was
watching whether it was still being used.

The delay is a setting rather than a constant, because it is a judgement about the
organisation's own risk, not a technical fact. An association working from a shared
parish computer wants thirty minutes; one where everybody has their own laptop is fine
with a day. It is expressed in MINUTES so both are expressible, and zero switches the
whole thing off for an organisation that does not want it.

The refusal carries a header naming the reason, so the interface can say "closed after
a period without activity" instead of a bare "session expired", and bring the person
back to the sign-in screen rather than leaving them on a page that no longer works.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db

# Setting name, and the value used when the organisation has not chosen one. Four
# hours is long enough not to interrupt a working session, short enough that a
# forgotten screen does not stay open all night.
CLE_PARAMETRE = "session_inactivite_minutes"
DEFAUT_MINUTES = 240

# Below this, the setting would fight ordinary use: a form filled slowly, a document
# read, a phone call taken mid-task. Refused at the write rather than accepted and
# regretted.
MINIMUM_MINUTES = 5
MAXIMUM_MINUTES = 20160  # fourteen days


def minutes_configurees(role: str | None = None) -> int:
    """The inactivity delay in minutes, or 0 when the organisation switched it off."""
    try:
        row = db.fetch_one(
            "SELECT (valeur #>> '{}')::int AS n FROM parametre WHERE cle = %s",
            (CLE_PARAMETRE,), role=role,
        )
    except Exception:  # noqa: BLE001 - a settings read must never break authentication
        return DEFAUT_MINUTES
    if not row or row.get("n") is None:
        return DEFAUT_MINUTES
    valeur = int(row["n"])
    if valeur <= 0:
        return 0
    return max(MINIMUM_MINUTES, min(MAXIMUM_MINUTES, valeur))


def etat_session(sid: str, role: str | None = None) -> dict[str, Any] | None:
    """The session row plus whether it has been idle past the configured delay.

    Returns None when the session does not exist, is revoked or is closed, which the
    caller already treats as a refusal.
    """
    minutes = minutes_configurees(role)
    if minutes <= 0:
        ligne = db.fetch_one(
            "SELECT id FROM session WHERE id = %s AND revoque = false AND fin IS NULL",
            (sid,), role=role,
        )
        return {"ouverte": bool(ligne), "inactive": False, "minutes": 0} if ligne else None
    ligne = db.fetch_one(
        "SELECT id, "
        # The clock runs from the last sign of life, falling back to the opening of
        # the session for one that has not been touched since.
        "(now() - COALESCE(derniere_activite, cree_le)) > make_interval(mins => %s) AS inactive "
        "FROM session WHERE id = %s AND revoque = false AND fin IS NULL",
        (minutes, sid), role=role,
    )
    if not ligne:
        return None
    return {"ouverte": True, "inactive": bool(ligne.get("inactive")), "minutes": minutes}


def marquer_activite(sid: str, role: str | None = None) -> None:
    """Record that the session is alive.

    Written at most once a minute: every authenticated call would otherwise be a
    write, turning a read-heavy screen into a write-heavy one for no gain, since the
    delay is measured in minutes.
    """
    try:
        db.execute(
            "UPDATE session SET derniere_activite = now() WHERE id = %s "
            "AND (derniere_activite IS NULL OR derniere_activite < now() - interval '1 minute')",
            (sid,), role=role,
        )
    except Exception:  # noqa: BLE001 - a missed heartbeat must never refuse a valid call
        pass


def fermer_pour_inactivite(sid: str, role: str | None = None) -> None:
    """Close the idle session so the same token cannot be presented again."""
    try:
        db.execute(
            "UPDATE session SET fin = now(), revoque = true WHERE id = %s AND fin IS NULL",
            (sid,), role=role,
        )
    except Exception:  # noqa: BLE001 - the refusal stands even if the row cannot be closed
        pass
