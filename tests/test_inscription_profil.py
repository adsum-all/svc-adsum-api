"""Regression tests for the member self-service profile update model (ProfilUpdate).

They lock the fix for the two submission blockers: an empty string coming from an
unselected dropdown must never reach a uuid / enum / CHECK-constrained column (which
raised a 500 at save time), and an invalid enum value must be refused up front (422)
rather than reaching the database.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.inscription import ProfilUpdate


def test_empty_strings_become_none() -> None:
    m = ProfilUpdate(
        intendance_id="",
        commission_id="",
        tribu_id="   ",
        situation_matrimoniale="",
        type_membre="",
        type_mariage="",
        genre="",
        region="",
    )
    d = m.model_dump(exclude_unset=True)
    vides = ("intendance_id", "commission_id", "tribu_id", "situation_matrimoniale",
             "type_membre", "type_mariage", "genre", "region")
    for champ in vides:
        assert d[champ] is None, f"{champ} should be None, got {d[champ]!r}"


def test_real_values_are_preserved() -> None:
    m = ProfilUpdate(
        commission_id="a3f1",
        ville="Abidjan",
        genre="femme",
        situation_matrimoniale="marie",
        type_membre="membre_actif",
        type_mariage="religieux",
    )
    d = m.model_dump(exclude_unset=True)
    assert d["commission_id"] == "a3f1"
    assert d["ville"] == "Abidjan"
    assert d["genre"] == "femme"
    assert d["situation_matrimoniale"] == "marie"
    assert d["type_membre"] == "membre_actif"
    assert d["type_mariage"] == "religieux"


@pytest.mark.parametrize(
    ("champ", "valeur"),
    [
        ("genre", "xyz"),
        ("situation_matrimoniale", "pacse"),
        ("type_mariage", "coutumier"),
        ("type_membre", "1invalide"),
    ],
)
def test_invalid_enum_is_rejected(champ: str, valeur: str) -> None:
    with pytest.raises(ValidationError):
        ProfilUpdate(**{champ: valeur})


def test_all_fields_optional_by_default() -> None:
    # An empty update is valid (nothing set): the coercion never turns absence into
    # an error, so a partial PATCH keeps working.
    m = ProfilUpdate()
    assert m.model_dump(exclude_unset=True) == {}
