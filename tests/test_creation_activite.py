"""Creating an activity must work, and keep working.

It did not, for as long as it took someone to try. A column was added to the INSERT's
column list and to its parameter tuple, and forgotten in its VALUES list. Nothing
catches that: an SQL statement is a string, so no type checker reads it, and the
failure only appears when the statement runs. Every attempt to create an activity, from
the back office or anywhere else, returned a 500 in the meantime.

The test creates a real activity and deletes it, because the only way to know the three
lists agree is to let the database compare them.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


def _contexte():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    from app.security import create_access_token

    compte = db.fetch_one(
        "SELECT id, role FROM utilisateur WHERE coalesce(acces_technique_global, false) AND actif LIMIT 1", ()
    )
    if compte is None:
        pytest.skip("aucun compte technique actif")
    return TestClient(app), {"Authorization": "Bearer " + create_access_token(str(compte["id"]), str(compte["role"]))}


def test_une_activite_simple_se_cree() -> None:
    from app import db

    client, entete = _contexte()
    debut = datetime.now(UTC) + timedelta(days=400)
    titre = "Contrôle automatique de création d'activité"
    db.execute("DELETE FROM evenement WHERE titre = %s", (titre,))
    try:
        reponse = client.post(
            "/api/v1/admin/evenements",
            headers=entete,
            json={
                "titre": titre,
                "debut": debut.isoformat(),
                "fin": (debut + timedelta(hours=2)).isoformat(),
                "lieu": "Contrôle",
                "mode": "hybride",
                "cible_type": "general",
                "visibilite": "membres",
                "fenetre_reponse_minutes": 120,
                # The field whose addition broke the statement. Exercised on purpose.
                "intervenant_principal": "Contrôle",
                "description": "Activité créée par le contrôle automatique.",
            },
        )
        assert reponse.status_code == 201, reponse.text
        cree = reponse.json()
        assert cree["titre"] == titre
    finally:
        db.execute("DELETE FROM evenement WHERE titre = %s", (titre,))


def test_une_serie_recurrente_se_cree() -> None:
    """The occurrence INSERT was short by one placeholder too, and is a separate statement."""
    from app import db

    client, entete = _contexte()
    debut = datetime.now(UTC) + timedelta(days=401)
    titre = "Contrôle automatique de série récurrente"
    db.execute("DELETE FROM evenement WHERE titre = %s", (titre,))
    try:
        reponse = client.post(
            "/api/v1/admin/evenements",
            headers=entete,
            json={
                "titre": titre,
                "debut": debut.isoformat(),
                "fin": (debut + timedelta(hours=1)).isoformat(),
                "cible_type": "general",
                "fenetre_reponse_minutes": 60,
                "occurrences": [
                    {
                        "debut": (debut + timedelta(days=7)).isoformat(),
                        "fin": (debut + timedelta(days=7, hours=1)).isoformat(),
                    },
                    {
                        "debut": (debut + timedelta(days=14)).isoformat(),
                        "fin": (debut + timedelta(days=14, hours=1)).isoformat(),
                    },
                ],
            },
        )
        assert reponse.status_code == 201, reponse.text
        compte = db.fetch_one("SELECT count(*) AS n FROM evenement WHERE titre = %s", (titre,))
        assert compte is not None
        assert int(compte["n"]) == 3, "la série doit compter l'activité maîtresse et ses deux occurrences"
    finally:
        db.execute("DELETE FROM evenement WHERE titre = %s", (titre,))
