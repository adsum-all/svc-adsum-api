"""The perimeter Scope value object and its SQL predicate (pure, no I/O).

Kept free of auth/db imports so the security-critical predicate logic can be
unit-tested in isolation and reused without pulling the whole app. The resolver
that builds a Scope from the database lives in perimetre.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    """The set of organisational units a responsable governs (closure included).

    ``commission_ids`` is an INTERSECTION filter (AND), not a governed axis: a
    commission responsable sees the members of their commission(s) that are also
    inside their own perimeter (coordination/intendance), never the same
    commission in another perimeter. It is empty for a perimeter responsable
    (coordinateur / intendant), who sees every member of their perimeter.
    """

    is_global: bool
    coordination_ids: frozenset[str]
    intendance_ids: frozenset[str]
    tribu_ids: frozenset[str]
    commission_ids: frozenset[str] = frozenset()

    def is_empty(self) -> bool:
        # A commission filter alone (no perimeter axis) is deliberately treated as
        # empty: without a perimeter boundary a commission responsable must never
        # see their commission across all perimeters. Perimeter is mandatory.
        return not (self.is_global or self.coordination_ids or self.intendance_ids or self.tribu_ids)

    def couvre(self, dimension: str, unit_id: str | None) -> bool:
        """Whether a specific organisational unit is inside this scope (non-general)."""
        if self.is_global:
            return True
        if not unit_id:
            return False
        by_dim = {
            "coordination": self.coordination_ids,
            "intendance": self.intendance_ids,
            "tribu": self.tribu_ids,
        }
        return unit_id in by_dim.get(dimension, frozenset())

    def membre_predicate(self, alias: str = "m") -> tuple[str, list[object]]:
        """SQL fragment + params selecting the members inside this scope.

        A member is in scope when their attachment on any governed perimeter axis
        falls in the scope's id set (union across axes) AND, when a commission
        filter is present, their commission is one of the governed commissions
        (intersection). Global scope matches everyone; an empty scope matches no
        one.
        """
        if self.is_global:
            return "TRUE", []
        clauses: list[str] = []
        params: list[object] = []
        for axis, ids in (
            ("coordination_id", self.coordination_ids),
            ("intendance_id", self.intendance_ids),
            ("tribu_id", self.tribu_ids),
        ):
            if ids:
                clauses.append(f"{alias}.{axis} = ANY(%s::uuid[])")
                params.append(list(ids))
        if not clauses:
            return "FALSE", []
        perimeter = "(" + " OR ".join(clauses) + ")"
        if self.commission_ids:
            perimeter = f"({perimeter} AND {alias}.commission_id = ANY(%s::uuid[]))"
            params.append(list(self.commission_ids))
        return perimeter, params
