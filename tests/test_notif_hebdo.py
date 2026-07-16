"""Weekly digest week-boundary maths (pure functions, no fabricated data)."""
from __future__ import annotations

from datetime import UTC, datetime

from app import notifications as N


def test_debut_semaine_lundi() -> None:
    # Wednesday 2026-07-15 -> week starting Monday 2026-07-13 at 00:00 (jour_debut=0).
    d = N._debut_semaine_locale(datetime(2026, 7, 15, 14, 30, tzinfo=UTC), 0)
    assert (d.year, d.month, d.day, d.hour) == (2026, 7, 13, 0)


def test_debut_semaine_samedi_configurable() -> None:
    # Same Wednesday, org whose week starts Saturday (jour_debut=5) -> Saturday 2026-07-11.
    d = N._debut_semaine_locale(datetime(2026, 7, 15, 14, 30, tzinfo=UTC), 5)
    assert (d.year, d.month, d.day) == (2026, 7, 11)


def test_debut_semaine_is_midnight_local() -> None:
    d = N._debut_semaine_locale(datetime(2026, 7, 13, 9, 0, tzinfo=UTC), 0)
    assert (d.hour, d.minute, d.second) == (0, 0, 0)
