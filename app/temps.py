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

# The zone used when nothing else says otherwise. Read from the organisation's own
# settings rather than fixed: org_fuseau was declared, editable in the back office and
# never read by anything, so a parish in France saw its times rendered in Abidjan.
# The literal below is only the fallback for a base that has not chosen.
_FUSEAU_PAR_DEFAUT = "Africa/Abidjan"


def fuseau_organisation() -> str:
    """The organisation's own zone, or the neutral fallback."""
    try:
        from . import db

        ligne = db.fetch_one(
            "SELECT valeur FROM integration_config WHERE cle = 'org_fuseau'", ()
        )
    except Exception:  # noqa: BLE001 - a settings read must never break a rendering
        return _FUSEAU_PAR_DEFAUT
    valeur = ((ligne or {}).get("valeur") or "").strip()
    if not valeur:
        return _FUSEAU_PAR_DEFAUT
    try:
        ZoneInfo(valeur)
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown zone would raise on every rendering; the fallback keeps the
        # message readable and the setting visibly wrong rather than fatal.
        return _FUSEAU_PAR_DEFAUT
    return valeur

DEFAULT_TZ = _FUSEAU_PAR_DEFAUT  # conservé pour les imports existants

_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

# Human, non-technical zone label: the country, so a member never mistakes an
# offset. "heure de France" is clear; "(UTC+2)" makes people add 2 hours again.
_PAYS_PAR_ZONE = {
    "Africa/Abidjan": "Côte d'Ivoire", "Europe/Paris": "France", "America/New_York": "États-Unis (Est)",
    "America/Chicago": "États-Unis (Centre)", "America/Los_Angeles": "États-Unis (Ouest)",
    "America/Toronto": "Canada", "America/Montreal": "Canada", "Europe/London": "Royaume-Uni",
    "Europe/Brussels": "Belgique", "Europe/Zurich": "Suisse", "Europe/Madrid": "Espagne",
    "Europe/Rome": "Italie", "Europe/Berlin": "Allemagne", "Africa/Dakar": "Sénégal",
    "Africa/Accra": "Ghana", "Africa/Lagos": "Nigéria", "Africa/Douala": "Cameroun",
    "Africa/Libreville": "Gabon", "Africa/Cotonou": "Bénin", "Africa/Lome": "Togo",
    "Africa/Niamey": "Niger", "Africa/Ouagadougou": "Burkina Faso", "Africa/Bamako": "Mali",
    "Africa/Conakry": "Guinée", "Africa/Kinshasa": "RD Congo", "Africa/Brazzaville": "Congo",
    "Africa/Casablanca": "Maroc", "Africa/Tunis": "Tunisie", "Africa/Algiers": "Algérie",
    "Africa/Nairobi": "Kenya", "Africa/Johannesburg": "Afrique du Sud",
}


def pays_du_fuseau(fuseau: str | None) -> str:
    """A human country label for an IANA zone, e.g. 'France', with a city fallback."""
    if not zone_valide(fuseau):
        return _PAYS_PAR_ZONE.get(fuseau_organisation(), _PAYS_PAR_ZONE[_FUSEAU_PAR_DEFAUT])
    if fuseau in _PAYS_PAR_ZONE:
        return _PAYS_PAR_ZONE[fuseau]
    return (fuseau or "").split("/")[-1].replace("_", " ")


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
        return ZoneInfo(fuseau or fuseau_organisation())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_FUSEAU_PAR_DEFAUT)


def local_datetime(instant: datetime, fuseau: str | None) -> datetime:
    """Convert an absolute instant to the given IANA zone, so a ``strftime`` renders
    the correct local date and time (never raw UTC). A naive instant is assumed UTC."""
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo("UTC"))
    return instant.astimezone(_zone(fuseau))


def formater_instant(instant: datetime | None, fuseau: str | None, avec_zone: bool = True) -> str:
    """Format an absolute instant in the recipient's zone, in French.

    The time is already the recipient's own local time; the label names the
    COUNTRY, never a UTC offset, so nobody adds hours by mistake. Example: an
    instant of 12:00 UTC shown to a member in Europe/Paris reads
    ``mardi 5 juillet 2026 à 14:00 (heure de France)``.
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
    return f"{base} (heure de {pays_du_fuseau(fuseau)})" if avec_zone else base
