"""Personal address in notifications: honorific title, pastoral name, or plain first name."""
from __future__ import annotations

from app import notifications as N


def _patch(monkeypatch, membre_row, fh_row):
    calls = iter([membre_row, fh_row])
    monkeypatch.setattr(N.db, "fetch_one", lambda *a, **k: next(calls))


def test_plain_first_name_normalised(monkeypatch) -> None:
    # No confirmed function, not a berger: greet by the first name, correct casing.
    _patch(monkeypatch, {"prenoms": "jean paul", "nom": "kouassi", "genre": "homme"}, None)
    a = N._appellation("M", None)
    assert a["prenom"] == "Jean" and a["appellation"] == "Jean"


def test_honorific_title_for_confirmed_function(monkeypatch) -> None:
    # A confirmed primary function with an honorific label -> "Pasteur Jean".
    _patch(
        monkeypatch,
        {"prenoms": "Jean", "nom": "Kouassi", "genre": "homme"},
        {"h": "Pasteur", "f": "Pasteure", "n": "Pasteur"},
    )
    a = N._appellation("M", None)
    assert a["appellation"] == "Pasteur Jean"
    assert a["prenom"] == "Pasteur Jean"  # so "Bonjour {prenom}" greets with the title


def test_female_title(monkeypatch) -> None:
    _patch(
        monkeypatch,
        {"prenoms": "Marie", "nom": "Ama", "genre": "femme"},
        {"h": "Pasteur", "f": "Pasteure", "n": "Pasteur"},
    )
    a = N._appellation("M", None)
    assert a["appellation"] == "Pasteure Marie"


def test_berger_pastoral_name_takes_precedence(monkeypatch) -> None:
    _patch(
        monkeypatch,
        {"prenoms": "David", "nom": "N", "genre": "homme", "est_berger": True, "nom_pastoral": "David"},
        {"h": "Pasteur", "f": "Pasteure", "n": "Pasteur"},
    )
    a = N._appellation("M", None)
    assert "David" in a["appellation"] and a["appellation"] != "Pasteur David"


def test_defensive_fallback_on_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(N.db, "fetch_one", boom)
    a = N._appellation("M", None)
    assert a["prenom"] == "cher membre"
