"""Unit tests for the birthday calendar overlay query builder.

They pin the fix that makes a member always see their OWN birthday on their own
calendar: the 'moi' category matches the caller by id and never applies the
peer-directory visibility filter, while every collective category still hides
members who opted out of the directory. No database is touched.
"""
from __future__ import annotations

from app.anniversaires_annuaire import _VISIBLE_CLAUSE, _categorie_where

_VIS = _VISIBLE_CLAUSE.strip()


def test_moi_matches_caller_and_ignores_visibility() -> None:
    built = _categorie_where("moi", "M-123")
    assert built is not None
    where, params = built
    assert "m.id = %s" in where
    assert _VIS not in where  # self view is never gated by the peer opt-out
    assert params == ["M-123"]


def test_collective_categories_keep_the_visibility_filter() -> None:
    for categorie in ("vip", "responsables", "direction", "coordinateurs", "bergers", "patriarches"):
        built = _categorie_where(categorie, "M-123")
        assert built is not None, categorie
        where, _ = built
        assert _VIS in where, f"{categorie} must keep the peer visibility filter"


def test_own_unit_categories_are_deferred_to_the_caller() -> None:
    for categorie in ("commission", "tribu", "coordination", "intendance"):
        assert _categorie_where(categorie, "M-123") is None
