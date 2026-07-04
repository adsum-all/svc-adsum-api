"""Unit tests for the civil identity single source of truth (no database)."""
from __future__ import annotations

import pytest

from app import identite


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  amoussou ", "AMOUSSOU"), ("Jean-Baptiste", "JEAN-BAPTISTE"), ("", ""), (None, "")],
)
def test_normaliser_nom(raw: str | None, expected: str) -> None:
    assert identite.normaliser_nom(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("marie claire", "Marie Claire"),
        ("EMMANUEL", "Emmanuel"),
        ("jean-paul", "Jean-Paul"),
        ("n'guessan", "N'Guessan"),
        ("david de jesus", "David de Jesus"),
        ("  marie   claire  ", "Marie Claire"),
    ],
)
def test_normaliser_prenoms(raw: str, expected: str) -> None:
    assert identite.normaliser_prenoms(raw) == expected


@pytest.mark.parametrize(
    ("nom", "prenoms", "expected"),
    [
        ("label", "shema emmanuel", "Shema Emmanuel LABEL"),
        ("label", "shema emmanuel jean paul", "Shema Emmanuel LABEL"),  # given names capped at 2
        ("MARCHAND", "Sophie", "Sophie MARCHAND"),
        (None, "Sophie", "Sophie"),
        ("DUPONT", None, "DUPONT"),
        (None, None, ""),
    ],
)
def test_nom_affichage_never_endless_given_names_first(nom: str | None, prenoms: str | None, expected: str) -> None:
    assert identite.nom_affichage(nom, prenoms) == expected


def test_nom_affichage_never_contains_a_function() -> None:
    # The civil name is built only from given + family names; a function label
    # passed by mistake would only appear if concatenated elsewhere, never here.
    out = identite.nom_affichage("KONE", "Awa")
    assert "Responsable" not in out and "Berger" not in out and out == "Awa KONE"


@pytest.mark.parametrize(
    ("genre", "nom_pastoral", "expected"),
    [
        ("femme", "marie de jesus", "Bergère Marie de Jesus"),
        ("homme", "david", "Berger David"),
        ("autre", "paul", "Berger Paul"),
        ("homme", None, None),
        ("femme", "  ", None),
    ],
)
def test_nom_pastoral_gendered(genre: str, nom_pastoral: str | None, expected: str | None) -> None:
    assert identite.nom_pastoral_affichage(genre, nom_pastoral) == expected
