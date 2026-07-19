"""Unit tests for the liturgical engine: Easter and the movable feasts must match
the known dates of the General Roman Calendar. Pure computation, no database."""
from __future__ import annotations

from datetime import date

from app import liturgie

# Reference Easter Sundays (Gregorian) from published liturgical calendars.
EASTER = {
    2010: date(2010, 4, 4),
    2023: date(2023, 4, 9),
    2024: date(2024, 3, 31),
    2025: date(2025, 4, 20),
    2026: date(2026, 4, 5),
    2027: date(2027, 3, 28),
    2030: date(2030, 4, 21),
}


def test_date_paques_known_years() -> None:
    for annee, attendu in EASTER.items():
        assert liturgie.date_paques(annee) == attendu, annee


def test_fetes_mobiles_2025() -> None:
    m = liturgie.fetes_mobiles(2025)
    assert m["paques"] == date(2025, 4, 20)
    assert m["cendres"] == date(2025, 3, 5)
    assert m["rameaux"] == date(2025, 4, 13)
    assert m["jeudi_saint"] == date(2025, 4, 17)
    assert m["vendredi_saint"] == date(2025, 4, 18)
    assert m["ascension"] == date(2025, 5, 29)
    assert m["pentecote"] == date(2025, 6, 8)
    assert m["trinite"] == date(2025, 6, 15)
    assert m["saint_sacrement"] == date(2025, 6, 19)
    assert m["sacre_coeur"] == date(2025, 6, 27)


def test_avent_et_christ_roi_2025() -> None:
    assert liturgie.premier_dimanche_avent(2025) == date(2025, 11, 30)
    assert liturgie.date_christ_roi(2025) == date(2025, 11, 23)
    # Advent is always a Sunday.
    for annee in (2024, 2025, 2026, 2027):
        assert liturgie.premier_dimanche_avent(annee).weekday() == 6, annee
        assert liturgie.date_christ_roi(annee).weekday() == 6, annee


def test_occurrences_mobiles_filtered_and_sorted() -> None:
    occ = liturgie.occurrences_mobiles(2026, cles_actives={"paques", "pentecote", "cendres"})
    assert [o["cle"] for o in occ] == ["cendres", "paques", "pentecote"]
    # sorted by date
    dates = [o["date"] for o in occ]
    assert dates == sorted(dates)
    # every occurrence carries the traceability stamp
    assert all(o["algorithme_version"] == liturgie.ALGORITHME_VERSION for o in occ)


def test_occurrences_mobiles_all_keys() -> None:
    occ = liturgie.occurrences_mobiles(2026)
    assert len(occ) == len(liturgie.CLES_MOBILES)
    assert {o["categorie"] for o in occ} == {"liturgie"}
