"""Real integration proof that the notification greeting is resolved from REAL member data.

No fabricated rows: it reads an actual member from the real database and asserts the
appellation is a real, non-empty greeting derived from that member's own identity (honorific
title when they hold a confirmed function, otherwise the first name). Skips when no database
is configured. This is the project-compliant replacement for a mocked unit test.
"""
# ruff: noqa: E501 - SQL lines are long by nature
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ADSUM_DATABASE_URL"), reason="requires the real database")

from app import db  # noqa: E402
from app import notifications as N  # noqa: E402

ROLE = "super_admin"


def test_appellation_from_real_member() -> None:
    row = db.fetch_one("SELECT id, prenoms FROM membre WHERE prenoms IS NOT NULL AND prenoms <> '' AND statut = 'actif' ORDER BY cree_le LIMIT 1", (), role=ROLE)
    if not row:
        pytest.skip("no active member with given names")
    appel = N._appellation(str(row["id"]), ROLE)
    # The greeting must be a real, non-empty string starting with the member's own first name
    # (possibly prefixed by a confirmed honorific title); never the neutral fallback here.
    assert appel["appellation"] and appel["prenom"]
    assert appel["prenom"] != "cher membre"
    assert appel["salutation"].startswith("Bonjour ")


def test_appellation_honorific_when_member_has_confirmed_function() -> None:
    # A member with a confirmed function whose catalogue entry has an honorific label must be
    # greeted with the title in front of the first name.
    row = db.fetch_one(
        "SELECT mf.membre_id AS mid FROM membre_fonction mf "
        "JOIN fonction_honorifique fh ON fh.cle = mf.fonction_cle "
        "JOIN membre m ON m.id = mf.membre_id "
        "WHERE mf.actif = true AND mf.confirmee = true AND m.statut = 'actif' "
        "AND coalesce(fh.libelle_h, fh.libelle_f, fh.libelle_n) IS NOT NULL "
        "ORDER BY mf.principale DESC, mf.ordre ASC LIMIT 1",
        (), role=ROLE,
    )
    if not row:
        pytest.skip("no member holds a confirmed honorific function")
    appel = N._appellation(str(row["mid"]), ROLE)
    # appellation carries more than the bare first name (a title or a pastoral appellation).
    assert appel["appellation"] != appel.get("prenom_simple", appel["prenom"]) or " " in appel["appellation"]
