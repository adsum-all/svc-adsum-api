"""Unit tests for the perimeter scope predicate logic (no database).

These assert the SQL fragment the scope produces, which is the security boundary
applied by every scoped pilotage query. Getting this wrong is a data leak, so the
intersection (commission AND perimeter) and the empty/global cases are pinned.
"""
from __future__ import annotations

from app.scope import Scope


def _empty() -> Scope:
    return Scope(False, frozenset(), frozenset(), frozenset())


def test_global_matches_everyone() -> None:
    s = Scope(True, frozenset(), frozenset(), frozenset())
    where, params = s.membre_predicate("m")
    assert where == "TRUE"
    assert params == []
    assert not s.is_empty()


def test_empty_scope_matches_no_one() -> None:
    s = _empty()
    where, params = s.membre_predicate("m")
    assert where == "FALSE"
    assert params == []
    assert s.is_empty()


def test_coordination_scope_union() -> None:
    s = Scope(False, frozenset({"c1"}), frozenset(), frozenset())
    where, params = s.membre_predicate("m")
    assert where == "(m.coordination_id = ANY(%s::uuid[]))"
    assert params == [["c1"]]
    assert not s.is_empty()


def test_two_axes_are_ored() -> None:
    s = Scope(False, frozenset({"c1"}), frozenset({"i1"}), frozenset())
    where, _ = s.membre_predicate("m")
    assert "m.coordination_id = ANY(%s::uuid[])" in where
    assert "m.intendance_id = ANY(%s::uuid[])" in where
    assert " OR " in where
    assert " AND " not in where  # no commission filter here


def test_commission_is_intersected_with_perimeter() -> None:
    # A commission responsable: their coordination AND their commission.
    s = Scope(False, frozenset({"c1"}), frozenset(), frozenset(), frozenset({"comm1"}))
    where, params = s.membre_predicate("m")
    assert where == "((m.coordination_id = ANY(%s::uuid[])) AND m.commission_id = ANY(%s::uuid[]))"
    assert params == [["c1"], ["comm1"]]
    assert not s.is_empty()


def test_commission_without_perimeter_is_empty() -> None:
    # A commission filter with no perimeter axis must select nobody (never the
    # same commission across all perimeters).
    s = Scope(False, frozenset(), frozenset(), frozenset(), frozenset({"comm1"}))
    where, _ = s.membre_predicate("m")
    assert where == "FALSE"
    assert s.is_empty()


def test_couvre_only_governed_units() -> None:
    s = Scope(False, frozenset({"c1"}), frozenset({"i1"}), frozenset())
    assert s.couvre("coordination", "c1")
    assert not s.couvre("coordination", "cX")
    assert s.couvre("intendance", "i1")
    assert not s.couvre("intendance", "iX")
    assert not s.couvre("tribu", "t1")


def test_commission_union_axis_alone() -> None:
    # A commission/mission-scoped group: its members are in scope on their own,
    # a union axis (unlike the intersection commission which needs a perimeter).
    s = Scope(False, frozenset(), frozenset(), frozenset(), commission_union_ids=frozenset({"cm1"}))
    where, params = s.membre_predicate("m")
    assert where == "m.commission_id = ANY(%s::uuid[])"
    assert params == [["cm1"]]
    assert not s.is_empty()
    assert s.couvre("commission", "cm1")
    assert not s.couvre("commission", "cmX")


def test_commission_union_ored_with_perimeter() -> None:
    s = Scope(False, frozenset({"c1"}), frozenset(), frozenset(), commission_union_ids=frozenset({"cm1"}))
    where, _ = s.membre_predicate("m")
    assert "m.coordination_id = ANY(%s::uuid[])" in where
    assert "m.commission_id = ANY(%s::uuid[])" in where
    assert " OR " in where
