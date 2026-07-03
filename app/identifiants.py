"""Durable, non-saturating business identifiers.

Both the member matricule and the request reference are year-partitioned with a
per-year atomic counter, so the numeric space resets every year (it never
saturates) and generation is concurrency-safe: a single ``INSERT ... ON CONFLICT
DO UPDATE ... RETURNING`` serialises concurrent callers on the counter row, which
rules out the read-max-then-increment race the previous MAX+1 logic had.

Formats::

    matricule  ADS-YYYY-NNNNNN   (new members; legacy ADS-NNNNNN kept as is)
    reference  DEM-YYYY-NNNNNN   (requests)

Six digits give 999 999 new items per year, far beyond any realistic need and
expandable without breaking any value already assigned.
"""
from __future__ import annotations

from . import db

_MATRICULE_SQL = (
    "INSERT INTO matricule_compteur (annee, dernier) VALUES (extract(year FROM now())::int, 1) "
    "ON CONFLICT (annee) DO UPDATE SET dernier = matricule_compteur.dernier + 1 RETURNING annee, dernier"
)
_REFERENCE_SQL = (
    "INSERT INTO demande_compteur (annee, dernier) VALUES (extract(year FROM now())::int, 1) "
    "ON CONFLICT (annee) DO UPDATE SET dernier = demande_compteur.dernier + 1 RETURNING annee, dernier"
)


def _bump(sql: str, role: str | None) -> tuple[int, int]:
    """Atomically bump the relevant year counter and return (year, sequence)."""
    row = db.execute(sql, (), role=role)
    if not row:
        raise RuntimeError("identifier counter did not return a row")
    return int(row["annee"]), int(row["dernier"])


def next_matricule(role: str | None) -> str:
    """Return the next member matricule, ADS-YYYY-NNNNNN."""
    annee, seq = _bump(_MATRICULE_SQL, role)
    return f"ADS-{annee}-{seq:06d}"


def next_reference(role: str | None) -> str:
    """Return the next request reference, DEM-YYYY-NNNNNN."""
    annee, seq = _bump(_REFERENCE_SQL, role)
    return f"DEM-{annee}-{seq:06d}"
