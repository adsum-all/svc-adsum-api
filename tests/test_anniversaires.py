"""Unit tests for the birthday calendar overlay query builder.

They pin two properties, without touching the database:
- a member always sees their OWN birthday ('moi' matches the caller and never
  applies the peer-directory visibility filter);
- every function-based category is ADDITIVE: it keeps the legacy primary-function
  match (so nothing that showed before disappears) AND adds the full, confirmed
  membre_fonction set (migration 0059) so secondary responsibilities are matched;
  bergers additionally match the est_berger consecration title.
"""
from __future__ import annotations

from app.anniversaires_annuaire import _VISIBLE_CLAUSE, _categorie_where

_VIS = _VISIBLE_CLAUSE.strip()


def _where(categorie: str) -> str:
    built = _categorie_where(categorie, "M-123")
    assert built is not None, categorie
    return built[0]


def test_moi_matches_caller_and_ignores_visibility() -> None:
    built = _categorie_where("moi", "M-123")
    assert built is not None
    where, params = built
    assert "m.id = %s" in where
    assert _VIS not in where
    assert params == ["M-123"]


def test_collective_categories_keep_the_visibility_filter() -> None:
    for categorie in ("vip", "responsables", "bergers", "direction", "coordinateurs", "patriarches"):
        assert _VIS in _where(categorie), f"{categorie} must keep the peer visibility filter"


def test_vip_is_additive_legacy_or_multifunction() -> None:
    where = _where("vip")
    assert "fh.est_vip = true" in where          # legacy primary match preserved
    assert "membre_fonction" in where and "fh2.est_vip = true" in where  # plus multi


def test_bergers_matches_title_or_bergers_family_function() -> None:
    built = _categorie_where("bergers", "M-123")
    assert built is not None
    where, params = built
    assert "m.est_berger = true" in where          # canonical consecration title
    assert "membre_fonction" in where              # plus a confirmed bergers-family function
    assert params == ["bergers"]


def test_family_categories_additive_and_parameterized() -> None:
    built = _categorie_where("direction", "M-123")
    assert built is not None
    where, params = built
    assert "fh.famille = %s" in where and "membre_fonction" in where
    assert params == ["direction", "direction"]  # legacy filter + EXISTS filter
    assert _categorie_where("coordinateurs", "M-123")[1] == ["coordination", "coordination"]  # type: ignore[index]


def test_responsables_matches_type_legacy_or_function() -> None:
    built = _categorie_where("responsables", "M-123")
    assert built is not None
    where, params = built
    assert "m.type_membre = 'responsable'" in where
    assert "m.fonction_cle = 'responsable'" in where
    assert "membre_fonction" in where
    assert params == ["responsables"]


def test_own_unit_categories_are_deferred_to_the_caller() -> None:
    for categorie in ("commission", "tribu", "coordination", "intendance"):
        assert _categorie_where(categorie, "M-123") is None
