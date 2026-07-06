"""Durable, unique business identifiers.

Member matricule, single canonical format::

    ADS-{L1}{L2}-{NNNNNN}-{X}   e.g. ADS-AM-000001-Q

- L1 = first significant letter of the family name (uppercase, accent-stripped).
- L2 = first significant letter of the first given name (same rule).
- NNNNNN = a GLOBAL 6-digit sequence (Postgres sequence ``matricule_seq``), so the
  numeric part is unique on its own: the whole matricule is unique regardless of the
  initials or the trailing letter, and the DB unique index is the real guarantee.
- X = a random uppercase letter, a light differentiator / visual check character; it
  is not needed for uniqueness (NNNNNN already is), only for readability.

Generation is server side only and concurrency-safe: ``nextval`` never returns the
same value twice, so no read-then-increment race and no collision retry are needed.
A missing/empty name falls back to 'X' for that initial, so a matricule is always
producible. The matricule is IMMUTABLE once assigned (business reference used in
exports and history); a later name change does not recompute it.

The request reference keeps its year-partitioned format ``DEM-YYYY-NNNNNN``.
"""
from __future__ import annotations

import random
import string
import unicodedata

from . import db

MATRICULE_RE = r"^ADS-[A-Z]{2}-[0-9]{6}-[A-Z]$"


def premiere_lettre(valeur: str | None) -> str:
    """First A-Z letter of a value, accent-stripped and uppercased, else 'X'."""
    ascii_form = unicodedata.normalize("NFD", valeur or "").encode("ascii", "ignore").decode()
    for ch in ascii_form:
        if ch.isalpha():
            return ch.upper()
    return "X"


def initiales(nom: str | None, prenoms: str | None) -> str:
    """Two-letter code: family-name initial then first-given-name initial."""
    premier_prenom = (prenoms or "").strip().split(" ", 1)[0]
    return premiere_lettre(nom) + premiere_lettre(premier_prenom)


def _lettre_aleatoire() -> str:
    return random.choice(string.ascii_uppercase)  # noqa: S311 - non-cryptographic differentiator


def next_matricule(role: str | None, nom: str | None = None, prenoms: str | None = None) -> str:
    """Return the next member matricule in the single canonical format."""
    row = db.fetch_one("SELECT nextval('matricule_seq') AS n", (), role=role)
    if not row:
        raise RuntimeError("matricule sequence did not return a value")
    return f"ADS-{initiales(nom, prenoms)}-{int(row['n']):06d}-{_lettre_aleatoire()}"


_REFERENCE_SQL = (
    "INSERT INTO demande_compteur (annee, dernier) VALUES (extract(year FROM now())::int, 1) "
    "ON CONFLICT (annee) DO UPDATE SET dernier = demande_compteur.dernier + 1 RETURNING annee, dernier"
)


def next_reference(role: str | None) -> str:
    """Return the next request reference, DEM-YYYY-NNNNNN."""
    row = db.execute(_REFERENCE_SQL, (), role=role)
    if not row:
        raise RuntimeError("reference counter did not return a row")
    return f"DEM-{int(row['annee'])}-{int(row['dernier']):06d}"
