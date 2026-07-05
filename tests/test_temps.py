"""Unit tests for time-zone aware formatting of absolute instants."""
from __future__ import annotations

from datetime import datetime, timezone

from app.temps import DEFAULT_TZ, formater_instant, zone_valide

_NOON_UTC = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def test_paris_summer_is_utc_plus_2() -> None:
    # The owner's example: 12:00 UTC shows as 14:00 for a member in Europe/Paris.
    out = formater_instant(_NOON_UTC, "Europe/Paris")
    assert "14:00" in out
    assert "UTC+2" in out


def test_abidjan_is_utc() -> None:
    out = formater_instant(_NOON_UTC, "Africa/Abidjan")
    assert "12:00" in out
    assert "UTC+0" in out


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
