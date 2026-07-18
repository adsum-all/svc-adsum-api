"""Live, database-derived counters for the organisation chart.

Two lenses, both excluding inactive or archived members:

- ``effectif_unique``: distinct active members holding at least one active
  responsibility (a person counted once, whatever the cumul).
- ``affectations_actives``: the total of active responsibility records
  (``membre_fonction``), so a member holding three functions weighs three.

The gap between the two measures the cumul. Structural placement (a member's
intendance/coordination/commission/tribu) is reported apart, because the nested
columns express a single placement, not a cumul. ``anomalies`` lists the real
integrity gaps found in the data (never invented). Kept out of the router module
so the endpoint stays a thin call and the file-size budget holds.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db

_ANOMALIES: tuple[tuple[str, str, str], ...] = (
    (
        "plusieurs_fonctions_principales",
        "Membres portant plus d'une affectation principale active.",
        "SELECT count(*) AS n FROM (SELECT mf.membre_id FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
        "WHERE mf.actif = true AND mf.principale = true AND m.statut = 'actif' GROUP BY mf.membre_id HAVING count(*) > 1) s",
    ),
    (
        "fonction_expiree_active",
        "Affectations encore actives dont la date de fin est dépassée.",
        "SELECT count(*) AS n FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
        "WHERE mf.actif = true AND mf.date_fin IS NOT NULL AND mf.date_fin < CURRENT_DATE AND m.statut = 'actif'",
    ),
    (
        "fonction_non_confirmee",
        "Affectations actives en attente de confirmation par un administrateur.",
        "SELECT count(*) AS n FROM membre_fonction mf JOIN membre m ON m.id = mf.membre_id "
        "WHERE mf.actif = true AND mf.confirmee = false AND m.statut = 'actif'",
    ),
)


def calculer(role: str | None) -> dict[str, Any]:
    """Compute the organisation counters and integrity anomalies from the real data."""
    aff = db.fetch_one(
        """
        SELECT
          count(DISTINCT mf.membre_id) AS effectif_unique,
          count(*) AS affectations_actives,
          count(*) FILTER (WHERE mf.principale) AS principales,
          count(*) FILTER (WHERE NOT mf.principale) AS secondaires
        FROM membre_fonction mf
        JOIN membre m ON m.id = mf.membre_id
        WHERE mf.actif = true AND m.statut = 'actif'
        """,
        (), role=role,
    ) or {}
    cumul = db.fetch_one(
        """
        SELECT count(*) AS n FROM (
          SELECT mf.membre_id FROM membre_fonction mf
          JOIN membre m ON m.id = mf.membre_id
          WHERE mf.actif = true AND m.statut = 'actif'
          GROUP BY mf.membre_id HAVING count(*) >= 2
        ) s
        """,
        (), role=role,
    ) or {}
    place = db.fetch_one(
        """
        SELECT
          count(*) FILTER (WHERE intendance_id IS NOT NULL OR coordination_id IS NOT NULL
                              OR commission_id IS NOT NULL OR tribu_id IS NOT NULL) AS membres_places,
          count(*) FILTER (WHERE intendance_id IS NOT NULL) AS intendances,
          count(*) FILTER (WHERE coordination_id IS NOT NULL) AS coordinations,
          count(*) FILTER (WHERE commission_id IS NOT NULL) AS commissions,
          count(*) FILTER (WHERE tribu_id IS NOT NULL) AS tribus,
          count(*) FILTER (WHERE est_berger = true) AS bergers
        FROM membre WHERE statut = 'actif'
        """,
        (), role=role,
    ) or {}

    effectif = int(aff.get("effectif_unique") or 0)
    actives = int(aff.get("affectations_actives") or 0)
    anomalies: list[dict[str, Any]] = []
    for code, libelle, sql in _ANOMALIES:
        n = int((db.fetch_one(sql, (), role=role) or {}).get("n") or 0)
        if n > 0:
            anomalies.append({"code": code, "libelle": libelle, "nombre": n})

    return {
        "affectations": {
            "effectif_unique": effectif,
            "affectations_actives": actives,
            "membres_en_cumul": int(cumul.get("n") or 0),
            "ecart_cumul": max(actives - effectif, 0),
            "principales": int(aff.get("principales") or 0),
            "secondaires": int(aff.get("secondaires") or 0),
        },
        "placement": {
            "membres_places": int(place.get("membres_places") or 0),
            "intendances": int(place.get("intendances") or 0),
            "coordinations": int(place.get("coordinations") or 0),
            "commissions": int(place.get("commissions") or 0),
            "tribus": int(place.get("tribus") or 0),
            "bergers": int(place.get("bergers") or 0),
        },
        "anomalies": anomalies,
    }
