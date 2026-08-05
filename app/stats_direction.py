"""The direction dashboard's per-entity attendance breakdown.

Lifted out of stats_core so that module stays under the size the Constitution
allows. It is a coherent piece on its own: one endpoint's worth of logic, the
dimensions the direction screen offers, and the single query behind them. The
consolidation rule it depends on stays the source of truth in stats_core, imported
here rather than copied, so a bar on this screen keeps summing to the global cards.
"""
# ruff: noqa: E501 - long, readable SQL lines
from __future__ import annotations

from . import db
from .stats_core import COMPTE

#: How each direction breakdown dimension is expressed in SQL.
#:
#: Two families. Member dimensions read an attribute of the person, joined through
#: the member tables. "volet" is the only event dimension: it reads a property of the
#: activity, which is why the global consolidation below keeps the event id, not only
#: the member id. "continent" is the organisation's own notion, carried on its
#: coordinations and intendances, rather than a guess from a free-text country string
#: the base has no reference table to resolve.
_DIRECTION_DIMENSIONS: dict[str, str] = {
    "commission": "coalesce(c.nom, 'Sans commission')",
    "intendance": "coalesce(i.nom, 'Sans intendance')",
    "coordination": "coalesce(co.nom, cod.nom, 'Sans coordination')",
    "tribu": "coalesce(t.nom, 'Sans tribu')",
    "pays": "coalesce(nullif(btrim(m.pays), ''), 'Non renseigné')",
    "continent": "coalesce(co.continent, cod.continent, 'Non renseigné')",
    "volet": "coalesce(e.volet, 'Non renseigné')",
}


def dimensions_direction() -> tuple[str, ...]:
    """The breakdown dimensions the direction dashboard may ask for."""
    return tuple(_DIRECTION_DIMENSIONS)


def repartition_globale_par_entite(
    dimension: str, role: str | None, cross: str | None = None,
) -> list[dict[str, object]] | None:
    """Cumulative attendance across ALL events, broken down by an entity dimension.

    Same consolidation as :func:`repartition_globale`, deduplicated per (member,
    event) over both attendance tables, so a bar here sums to the global cards. When
    ``cross`` is given, each row is one combination of the two dimensions, which is
    what the direction screen's cross table reads.

    Returns None for an unknown dimension rather than guessing, so a caller sending a
    dimension this platform does not compute gets an honest "not available" instead of
    a plausible-looking but wrong breakdown. The label whitelist also keeps the
    dimension out of the SQL as anything but a known, fixed expression.
    """
    expr = _DIRECTION_DIMENSIONS.get(dimension)
    if expr is None:
        return None
    cross_expr = _DIRECTION_DIMENSIONS.get(cross) if cross else None
    if cross and cross_expr is None:
        return None

    # The label is the dimension, or "dimension - cross" when a second axis is asked.
    cle = f"{expr} || ' - ' || {cross_expr}" if cross_expr else expr
    rows = db.fetch_all(
        f"""
        WITH brut AS (
            SELECT pr.membre_id, pr.evenement_id, 'present'::text AS statut
            FROM presence pr
            UNION ALL
            SELECT pa.membre_id, pa.evenement_id, pa.statut
            FROM participation pa
            WHERE {COMPTE} AND pa.statut IN ('present', 'partiel', 'absent')
        ),
        conso AS (
            SELECT membre_id, evenement_id,
                   bool_or(statut = 'present') AS present,
                   bool_or(statut = 'partiel') AS partiel,
                   bool_or(statut = 'absent') AS absent
            FROM brut GROUP BY membre_id, evenement_id
        )
        SELECT {cle} AS cle,
               count(*) FILTER (WHERE present) AS presents,
               count(*) FILTER (WHERE partiel AND NOT present) AS partiels,
               count(*) FILTER (WHERE absent AND NOT present AND NOT partiel) AS absents
        FROM conso cc
        JOIN membre m ON m.id = cc.membre_id
        JOIN evenement e ON e.id = cc.evenement_id
        LEFT JOIN commission c ON c.id = m.commission_id
        LEFT JOIN intendance i ON i.id = m.intendance_id
        LEFT JOIN coordination co ON co.id = i.coordination_id
        LEFT JOIN coordination cod ON cod.id = m.coordination_id
        LEFT JOIN tribu t ON t.id = m.tribu_id
        GROUP BY {cle}
        ORDER BY presents DESC, cle ASC
        """,
        (),
        role=role,
    )
    return [
        {
            "label": r["cle"],
            "presents": int(r["presents"]),
            "partiels": int(r["partiels"]),
            "absents": int(r["absents"]),
        }
        for r in rows
    ]

