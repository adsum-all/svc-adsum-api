# ruff: noqa: E501
"""Catholic liturgical engine: computes the movable feasts of the General Roman
Calendar for any year, so they never have to be re-entered by hand.

Everything derives from the date of Easter Sunday, computed with the Gregorian
algorithm (Meeus/Jones/Butcher). The movable feasts are fixed offsets from Easter
(Ash Wednesday, Palm Sunday, the Paschal Triduum, Divine Mercy, Ascension,
Pentecost, Trinity, Corpus Christi, the Sacred Heart) plus two feasts anchored on
the Advent cycle (Christ the King and the First Sunday of Advent).

The engine is pure (no database, no I/O), so it is deterministic and unit-tested.
`ALGORITHME_VERSION` is stamped on generated occurrences for traceability; bump it
if the computation ever changes.
"""
from __future__ import annotations

from datetime import date, timedelta

ALGORITHME_VERSION = "roman-general-1"
SOURCE = "Calendrier romain général (Missel romain, 3e édition typique)"

# Movable feasts expressed as a signed day offset from Easter Sunday.
_OFFSETS_PAQUES: dict[str, int] = {
    "cendres": -46,
    "rameaux": -7,
    "jeudi_saint": -3,
    "vendredi_saint": -2,
    "samedi_saint": -1,
    "paques": 0,
    "lundi_paques": 1,
    "misericorde": 7,
    "ascension": 39,
    "pentecote": 49,
    "trinite": 56,
    "saint_sacrement": 60,
    "sacre_coeur": 68,
}

# Human labels and default liturgical rank/colour for every movable feast key.
_MOBILE_META: dict[str, dict[str, str]] = {
    "cendres": {"nom": "Mercredi des Cendres", "rang": "jour", "couleur": "lit_violet"},
    "rameaux": {"nom": "Dimanche des Rameaux", "rang": "dimanche", "couleur": "lit_rouge"},
    "jeudi_saint": {"nom": "Jeudi Saint, Cène du Seigneur", "rang": "triduum", "couleur": "lit_blanc"},
    "vendredi_saint": {"nom": "Vendredi Saint, Passion du Seigneur", "rang": "triduum", "couleur": "lit_rouge"},
    "samedi_saint": {"nom": "Samedi Saint", "rang": "triduum", "couleur": "lit_gris"},
    "paques": {"nom": "Dimanche de la Résurrection (Pâques)", "rang": "solennite", "couleur": "lit_pascal"},
    "lundi_paques": {"nom": "Lundi de Pâques", "rang": "octave", "couleur": "lit_pascal"},
    "misericorde": {"nom": "Dimanche de la Divine Miséricorde", "rang": "dimanche", "couleur": "lit_pascal"},
    "ascension": {"nom": "Ascension du Seigneur", "rang": "solennite", "couleur": "lit_blanc"},
    "pentecote": {"nom": "Pentecôte", "rang": "solennite", "couleur": "lit_rouge"},
    "trinite": {"nom": "Sainte Trinité", "rang": "solennite", "couleur": "lit_blanc"},
    "saint_sacrement": {"nom": "Saint-Sacrement (Fête-Dieu)", "rang": "solennite", "couleur": "lit_blanc"},
    "sacre_coeur": {"nom": "Sacré-Cœur de Jésus", "rang": "solennite", "couleur": "lit_blanc"},
    "christ_roi": {"nom": "Christ, Roi de l'univers", "rang": "solennite", "couleur": "lit_blanc"},
    "avent": {"nom": "1er dimanche de l'Avent", "rang": "dimanche", "couleur": "lit_violet"},
}

# Every movable feast key the engine can produce (stable order for display).
CLES_MOBILES: tuple[str, ...] = (
    "cendres", "rameaux", "jeudi_saint", "vendredi_saint", "samedi_saint", "paques",
    "lundi_paques", "misericorde", "ascension", "pentecote", "trinite",
    "saint_sacrement", "sacre_coeur", "christ_roi", "avent",
)


def date_paques(annee: int) -> date:
    """Easter Sunday for a Gregorian year (Meeus/Jones/Butcher algorithm)."""
    a = annee % 19
    b = annee // 100
    c = annee % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    lp = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lp) // 451
    mois = (h + lp - 7 * m + 114) // 31
    jour = ((h + lp - 7 * m + 114) % 31) + 1
    return date(annee, mois, jour)


def _dernier_dimanche_avant_ou_egal(reference: date) -> date:
    """The most recent Sunday on or before `reference`."""
    return reference - timedelta(days=(reference.weekday() - 6) % 7)


def premier_dimanche_avent(annee: int) -> date:
    """First Sunday of Advent: three Sundays before the 4th Sunday of Advent, the
    latter being the last Sunday on or before 24 December of the year."""
    quatrieme = _dernier_dimanche_avant_ou_egal(date(annee, 12, 24))
    return quatrieme - timedelta(days=21)


def date_christ_roi(annee: int) -> date:
    """Christ the King: the Sunday before the First Sunday of Advent."""
    return premier_dimanche_avent(annee) - timedelta(days=7)


def fetes_mobiles(annee: int) -> dict[str, date]:
    """All movable feast dates for the year, keyed by their stable key."""
    paques = date_paques(annee)
    out: dict[str, date] = {cle: paques + timedelta(days=delta) for cle, delta in _OFFSETS_PAQUES.items()}
    out["christ_roi"] = date_christ_roi(annee)
    out["avent"] = premier_dimanche_avent(annee)
    return out


def meta_mobile(cle: str) -> dict[str, str]:
    """Label, rank and default colour for a movable feast key."""
    return _MOBILE_META.get(cle, {"nom": cle.replace("_", " ").capitalize(), "rang": "jour", "couleur": "lit_violet"})


def occurrences_mobiles(annee: int, cles_actives: set[str] | None = None) -> list[dict[str, object]]:
    """Dated occurrences of the movable feasts for one year, optionally filtered to
    the keys enabled by the organisation. Each occurrence is calendar-ready."""
    dates = fetes_mobiles(annee)
    out: list[dict[str, object]] = []
    for cle in CLES_MOBILES:
        if cles_actives is not None and cle not in cles_actives:
            continue
        meta = meta_mobile(cle)
        out.append({
            "cle": cle,
            "nom": meta["nom"],
            "date": dates[cle].isoformat(),
            "rang": meta["rang"],
            "couleur": meta["couleur"],
            "type": "mobile",
            "categorie": "liturgie",
            "source": SOURCE,
            "algorithme_version": ALGORITHME_VERSION,
        })
    out.sort(key=lambda o: str(o["date"]))
    return out
