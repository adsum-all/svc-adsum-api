"""Unit tests for time-zone aware formatting of absolute instants."""
from __future__ import annotations

from datetime import UTC, datetime

from app.temps import DEFAULT_TZ, formater_instant, local_datetime, zone_valide

_NOON_UTC = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def test_local_datetime_shifts_day_across_midnight() -> None:
    """The notification date bug: a late-evening UTC instant is the NEXT day in a
    positive-offset zone. local_datetime must reflect that, so a reminder never
    tells a member the wrong day."""
    inst = datetime(2026, 7, 10, 23, 0, tzinfo=UTC)
    abidjan = local_datetime(inst, "Africa/Abidjan")  # UTC+0
    paris = local_datetime(inst, "Europe/Paris")      # UTC+2 in July
    assert (abidjan.day, abidjan.hour) == (10, 23)
    assert (paris.day, paris.hour) == (11, 1)


def test_local_datetime_naive_is_utc() -> None:
    naive = datetime(2026, 7, 10, 23, 0)
    assert local_datetime(naive, "Europe/Paris").day == 11


def test_paris_summer_is_local_time_with_country() -> None:
    # The owner's example: 12:00 UTC shows as 14:00 for a member in Europe/Paris,
    # labelled by country (never a UTC offset that people re-add by mistake).
    out = formater_instant(_NOON_UTC, "Europe/Paris")
    assert "14:00" in out
    assert "heure de France" in out
    assert "UTC" not in out


def test_abidjan_is_utc() -> None:
    out = formater_instant(_NOON_UTC, "Africa/Abidjan")
    assert "12:00" in out
    assert "Côte d'Ivoire" in out


def test_new_york_is_behind() -> None:
    out = formater_instant(_NOON_UTC, "America/New_York")
    assert "08:00" in out


def test_invalid_zone_falls_back_to_default() -> None:
    out = formater_instant(_NOON_UTC, "Not/AZone")
    assert formater_instant(_NOON_UTC, DEFAULT_TZ) == out


def test_naive_instant_is_treated_as_utc() -> None:
    naive = datetime(2026, 7, 5, 12, 0)
    assert "14:00" in formater_instant(naive, "Europe/Paris")


def test_zone_valide() -> None:
    assert zone_valide("Europe/Paris") == "Europe/Paris"
    assert zone_valide("America/New_York") == "America/New_York"
    assert zone_valide("bad") is None
    assert zone_valide(None) is None
    assert zone_valide("+02:00") is None  # fixed offsets are rejected


def test_none_instant() -> None:
    assert formater_instant(None, "Europe/Paris") == "-"
