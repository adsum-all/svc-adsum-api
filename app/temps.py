"""Time-zone aware formatting of absolute instants for server-rendered messages.

Activity times are stored as absolute instants (``timestamptz``); the browser
converts them to the viewer's local zone for the member apps. Server-rendered
text (Telegram, e-mail, survey notifications) has no browser, so it must convert
the instant into the recipient's own IANA zone here, using :mod:`zoneinfo` so
daylight-saving changes are handled correctly, never a hard-coded offset.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Africa/Abidjan"  # GMT+0, a safe neutral reference for the base

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def zone_valide(fuseau: str | None) -> str | None:
    """Return the IANA zone if it is a real one, else None (no fixed offsets)."""
    if not fuseau:
        return None
    try:
        ZoneInfo(fuseau)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return fuseau


def _zone(fuseau: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(fuseau or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _abbrev(local: datetime) -> str:
    """A short, human offset label such as 'UTC+2' for the localized instant."""
    offset = local.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    h, m = divmod(abs(total), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def formater_instant(instant: datetime | None, fuseau: str | None, avec_zone: bool = True) -> str:
    """Format an absolute instant in the recipient's zone, in French.

    Example: an instant of 12:00 UTC shown to a member in Europe/Paris (summer)
    reads ``mardi 5 juillet 2026 à 14:00 (UTC+2)``.
    """
    if instant is None:
        return "-"
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo("UTC"))
    local = instant.astimezone(_zone(fuseau))
    base = (
        f"{_JOURS[local.weekday()]} {local.day} {_MOIS[local.month - 1]} {local.year} "
        f"à {local.hour:02d}:{local.minute:02d}"
    )
    return f"{base} ({_abbrev(local)})" if avec_zone else base
