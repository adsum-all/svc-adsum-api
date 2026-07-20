"""Unit tests for the institutional-calendar pure helpers: next-occurrence
computation (used by the reminder cron) and the iCalendar serialisation. No DB."""
from __future__ import annotations

from datetime import date

from app import calendrier_institutionnel as ci


def test_prochaine_occurrence_recurrente_cette_annee() -> None:
    r = {"mois": 7, "jour": 18, "repetition_annuelle": True, "date_fixe": None}
    assert ci._prochaine_occurrence(r, date(2026, 1, 1)) == date(2026, 7, 18)


def test_prochaine_occurrence_recurrente_annee_suivante() -> None:
    # After this year's day has passed, the next occurrence is next year.
    r = {"mois": 7, "jour": 18, "repetition_annuelle": True, "date_fixe": None}
    assert ci._prochaine_occurrence(r, date(2026, 8, 1)) == date(2027, 7, 18)


def test_prochaine_occurrence_fixe_non_recurrente() -> None:
    r = {"mois": None, "jour": None, "repetition_annuelle": False, "date_fixe": date(2026, 12, 25)}
    assert ci._prochaine_occurrence(r, date(2026, 1, 1)) == date(2026, 12, 25)
    # A past non-recurring date yields no occurrence.
    assert ci._prochaine_occurrence(r, date(2027, 1, 1)) is None


def test_ics_contains_vevent_and_all_day_date() -> None:
    occ = [{
        "source_id": "abc", "origine": "institution", "titre": "Anniversaire, test",
        "date": "2026-07-18", "description": "<p>Bonjour; ligne</p>",
    }]
    text = ci._ics(occ, 2026)
    assert text.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in text
    assert "BEGIN:VEVENT" in text
    assert "DTSTART;VALUE=DATE:20260718" in text
    # ICS escaping of comma and semicolon in the summary/description.
    assert "SUMMARY:Anniversaire\\, test" in text
    assert "UID:institution-abc-2026@adsum" in text


def test_ics_escape() -> None:
    assert ci._ics_escape("a,b;c\\d") == "a\\,b\\;c\\\\d"
