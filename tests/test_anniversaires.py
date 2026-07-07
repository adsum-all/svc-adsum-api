"""Unit tests for the birthday calendar overlay query builder.

They pin two fixes, without touching the database:
- a member always sees their OWN birthday ('moi' matches the caller and never
  applies the peer-directory visibility filter);
- the category filters are ALIGNED with the canonical role definitions: a berger
  is the consecration title (est_berger), not a function; every function-based
  category (vip, responsables, direction, coordinateurs, patriarches) matches the
  member's full, confirmed membre_fonction set, not only the primary fonction_cle.
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
    for categorie in ("vip", "responsables", "bergers", "direction", "coordinateurs", "patriarches"):
        built = _categorie_where(categorie, "M-123")
        assert built is not None, categorie
        where, _ = built
        assert _VIS in where, f"{categorie} must keep the peer visibility filter"


def test_bergers_uses_the_consecration_flag_not_a_function() -> None:
    where, params = _categorie_where("bergers", "M-123")  # type: ignore[misc]
    assert "m.est_berger = true" in where
    assert "membre_fonction" not in where  # berger is never a function
    assert params == []


def test_function_categories_scan_all_confirmed_functions() -> None:
    # vip: any active+confirmed function flagged est_vip
    where, params = _categorie_where("vip", "M-123")  # type: ignore[misc]
    assert "membre_fonction" in where and "fh2.est_vip = true" in where
    assert params == []
    # direction/coordinateurs/patriarches: by function family, parameterized
    where_d, params_d = _categorie_where("direction", "M-123")  # type: ignore[misc]
    assert "membre_fonction" in where_d and "fh2.famille = %s" in where_d
    assert params_d == ["direction"]
    assert _categorie_where("coordinateurs", "M-123")[1] == ["coordination"]  # type: ignore[index]


def test_responsables_matches_type_or_function_family() -> None:
    where, params = _categorie_where("responsables", "M-123")  # type: ignore[misc]
    assert "m.type_membre = 'responsable'" in where
    assert "membre_fonction" in where
    assert params == ["responsables"]


def test_own_unit_categories_are_deferred_to_the_caller() -> None:
    for categorie in ("commission", "tribu", "coordination", "intendance"):
        assert _categorie_where(categorie, "M-123") is None
