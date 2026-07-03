"""Unit tests for the durable identifier generators (no database needed).

They prove the format (year-partitioned, six-digit, zero-padded) and that the
generator relies on an atomic UPSERT ... RETURNING on a per-year counter, which
is what makes it concurrency-safe and non-saturating.
"""
from __future__ import annotations

import pytest

from app import identifiants


class _FakeDB:
    """Stand-in for app.db that emulates the per-year atomic counter."""

    def __init__(self) -> None:
        self.counters: dict[tuple[str, int], int] = {}
        self.last_sql = ""

    def execute(self, sql: str, params: tuple[object, ...], role: str | None = None) -> dict[str, object]:
        self.last_sql = sql
        # Emulate: INSERT ... ON CONFLICT DO UPDATE dernier = dernier + 1, fixed year.
        table = "matricule_compteur" if "matricule_compteur" in sql else "demande_compteur"
        annee = 2026
        key = (table, annee)
        self.counters[key] = self.counters.get(key, 0) + 1
        return {"annee": annee, "dernier": self.counters[key]}


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    fake = _FakeDB()
    monkeypatch.setattr(identifiants, "db", fake)
    return fake


def test_matricule_format(fake_db: _FakeDB) -> None:
    assert identifiants.next_matricule("admin") == "ADS-2026-000001"
    assert identifiants.next_matricule("admin") == "ADS-2026-000002"
    # The new format never collides with the legacy ADS-NNNNNN shape.
    assert identifiants.next_matricule("admin") != "ADS-000003"


def test_reference_format(fake_db: _FakeDB) -> None:
    assert identifiants.next_reference("membre") == "DEM-2026-000001"
    assert identifiants.next_reference("membre") == "DEM-2026-000002"


def test_matricule_uses_atomic_upsert(fake_db: _FakeDB) -> None:
    identifiants.next_matricule("admin")
    sql = fake_db.last_sql.lower()
    assert "on conflict" in sql and "returning" in sql and "dernier + 1" in sql


def test_reference_monotonic(fake_db: _FakeDB) -> None:
    refs = [identifiants.next_reference("membre") for _ in range(5)]
    assert refs == [f"DEM-2026-{i:06d}" for i in range(1, 6)]
    assert len(set(refs)) == 5  # no collision


def test_six_digits_do_not_saturate_before_a_million(fake_db: _FakeDB) -> None:
    fake_db.counters[("demande_compteur", 2026)] = 999_998
    assert identifiants.next_reference("m") == "DEM-2026-999999"
    # Past six digits the number simply grows; it never wraps or collides.
    assert identifiants.next_reference("m") == "DEM-2026-1000000"
