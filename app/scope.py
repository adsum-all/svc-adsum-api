"""The perimeter Scope value object and its SQL predicate (pure, no I/O).

Kept free of auth/db imports so the security-critical predicate logic can be
unit-tested in isolation and reused without pulling the whole app. The resolver
that builds a Scope from the database lives in perimetre.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scope:
    """The set of organisational units a person governs (closure included).

    Two commission notions coexist, on purpose:

    - ``commission_ids`` is an INTERSECTION filter (AND): a commission responsable
      (via the function/``commission.responsable_id`` model) sees the members of
      their commission(s) that are ALSO inside their own perimeter, never the same
      commission across other perimeters. Empty for a pure perimeter responsable.
    - ``commission_union_ids`` is a UNION axis (OR): a member granted a
      commission/mission-scoped access GROUP sees the members of that commission,
      as one more governed axis alongside coordination/intendance/tribu.

    The union axes (coordination/intendance/tribu/commission_union) are OR-ed;
    the intersection commission narrows only the perimeter-axis part.
    """

    is_global: bool
    coordination_ids: frozenset[str]
    intendance_ids: frozenset[str]
    tribu_ids: frozenset[str]
    commission_ids: frozenset[str] = frozenset()
    commission_union_ids: frozenset[str] = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        # A pure intersection commission filter (no perimeter axis) is deliberately
        # empty: without a perimeter boundary a commission responsable must never
        # see their commission across all perimeters. A union commission axis, on
        # the contrary, is a governed axis and makes the scope non-empty.
        return not (
            self.is_global
            or self.coordination_ids
            or self.intendance_ids
            or self.tribu_ids
            or self.commission_union_ids
        )

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
            "commission": self.commission_union_ids,
        }
        return unit_id in by_dim.get(dimension, frozenset())

    def membre_predicate(self, alias: str = "m") -> tuple[str, list[object]]:
        """SQL fragment + params selecting the members inside this scope.

        A member is in scope when their attachment on any governed union axis
        (coordination / intendance / tribu / commission-union) matches, and, when a
        function-based intersection commission is present, their commission is one
        of those governed commissions (narrowing only the perimeter-axis part).
        Global scope matches everyone; an empty scope matches no one.
        """
        if self.is_global:
            return "TRUE", []
        params: list[object] = []

        # Perimeter axes (coordination / intendance / tribu), OR-ed together.
        perimeter_clauses: list[str] = []
        for axis, ids in (
            ("coordination_id", self.coordination_ids),
            ("intendance_id", self.intendance_ids),
            ("tribu_id", self.tribu_ids),
        ):
            if ids:
                perimeter_clauses.append(f"{alias}.{axis} = ANY(%s::uuid[])")
                params.append(list(ids))
        perimeter_part = "(" + " OR ".join(perimeter_clauses) + ")" if perimeter_clauses else ""
        if perimeter_part and self.commission_ids:
            perimeter_part = f"({perimeter_part} AND {alias}.commission_id = ANY(%s::uuid[]))"
            params.append(list(self.commission_ids))

        # Commission-union axis (group-scoped): those members join the scope too.
        union_parts: list[str] = []
        if perimeter_part:
            union_parts.append(perimeter_part)
        if self.commission_union_ids:
            union_parts.append(f"{alias}.commission_id = ANY(%s::uuid[])")
            params.append(list(self.commission_union_ids))

        if not union_parts:
            return "FALSE", []
        if len(union_parts) == 1:
            return union_parts[0], params
        return "(" + " OR ".join(union_parts) + ")", params
