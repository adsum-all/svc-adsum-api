"""Unit tests for the durable identifier generators (no database needed).

They prove the single canonical matricule format ADS-{L1}{L2}-{NNNNNN}-{X} (initials
from the name, global 6-digit sequence, random trailing letter) and that the request
reference keeps its year-partitioned atomic format.
"""
from __future__ import annotations

import re

import pytest

from app import identifiants


class _FakeDB:
    """Stand-in for app.db: a global sequence (fetch_one) and a per-year counter (execute)."""

    def __init__(self) -> None:
        self.seq = 0
        self.counters: dict[tuple[str, int], int] = {}

    def fetch_one(self, sql: str, params: tuple[object, ...] = (), role: str | None = None) -> dict[str, object]:
        assert "nextval" in sql and "matricule_seq" in sql
        self.seq += 1
        return {"n": self.seq}

    def execute(self, sql: str, params: tuple[object, ...] = (), role: str | None = None) -> dict[str, object]:
        annee = 2026
        key = ("demande_compteur", annee)
        self.counters[key] = self.counters.get(key, 0) + 1
        return {"annee": annee, "dernier": self.counters[key]}


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    fake = _FakeDB()
    monkeypatch.setattr(identifiants, "db", fake)
    return fake


# --- Initials -------------------------------------------------------------

def test_initiales_basic() -> None:
    assert identifiants.initiales("AMOUSSOU", "Armand") == "AA"
    assert identifiants.initiales("Brou", "Emmanuel Kouassi") == "BE"


def test_initiales_strip_accents_and_case() -> None:
    assert identifiants.initiales("Éboué", "Ózrïc") == "EO"
    assert identifiants.initiales("  koffi ", "  jean-baptiste") == "KJ"


def test_initiales_fallback_on_missing() -> None:
    assert identifiants.initiales(None, None) == "XX"
    assert identifiants.initiales("", "Marie") == "XM"
    assert identifiants.initiales("Traore", "") == "TX"


def test_initiales_leading_symbol() -> None:
    # Leading symbols are skipped to the first real letter.
    assert identifiants.initiales("'Ndiaye", "@lain") == "NL"
    assert identifiants.initiales("-Traore", "Alain") == "TA"


def test_premiere_lettre_no_letter() -> None:
    assert identifiants.premiere_lettre("123-!") == "X"
    assert identifiants.premiere_lettre(None) == "X"


# --- Matricule format -----------------------------------------------------

def test_matricule_matches_canonical_format(fake_db: _FakeDB) -> None:
    m = identifiants.next_matricule("admin", "AMOUSSOU", "Armand")
    assert re.fullmatch(identifiants.MATRICULE_RE, m), m
    assert m.startswith("ADS-AA-000001-")


def test_matricule_sequence_is_unique(fake_db: _FakeDB) -> None:
    seen = {identifiants.next_matricule("admin", "Traore", "Marie")[7:13] for _ in range(20)}
    assert len(seen) == 20  # the 6-digit part never repeats


def test_matricule_regex_rejects_legacy() -> None:
    for bad in ("ADS-000001", "ADS-2026-000001", "ADS-A-000001-Q", "ADS-AM-00001-Q", "ads-am-000001-q"):
        assert not re.fullmatch(identifiants.MATRICULE_RE, bad), bad


# --- Reference (unchanged) ------------------------------------------------

def test_reference_format(fake_db: _FakeDB) -> None:
    assert identifiants.next_reference("membre") == "DEM-2026-000001"
    assert identifiants.next_reference("membre") == "DEM-2026-000002"
