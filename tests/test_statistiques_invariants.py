"""Invariants a published figure must always satisfy, checked on real activities.

A statistic that is merely computed is not yet correct. These assert the two properties
whose absence produced wrong numbers, on every activity that actually carries
evaluations, so a regression is caught by the data rather than by a reviewer.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


def _client_et_entete():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    from app.security import create_access_token

    compte = db.fetch_one(
        "SELECT id, role FROM utilisateur WHERE coalesce(acces_technique_global, false) AND actif LIMIT 1", ()
    )
    if compte is None:
        pytest.skip("aucun compte technique actif pour interroger l'API")
    jeton = create_access_token(str(compte["id"]), str(compte["role"]))
    return TestClient(app), {"Authorization": f"Bearer {jeton}"}


def _activites_evaluees() -> list[str]:
    from app import db

    return [
        str(r["evenement_id"])
        for r in db.fetch_all("SELECT DISTINCT evenement_id FROM evaluation_activite", ())
    ]


def test_la_moyenne_n_est_diffusee_qu_au_dela_du_seuil_de_notes() -> None:
    """The anonymity threshold must count the ratings that build the mean.

    Counting every evaluation instead published the mean as soon as three people
    answered, even when a single one had rated: the "average" was that person's own
    score, on an activity whose participants are known. Two people can then identify
    the third.
    """
    from app.participation import _SEUIL_EVAL

    client, entete = _client_et_entete()
    activites = _activites_evaluees()
    assert activites, "aucune activité évaluée en base : le contrôle serait vide"

    for evenement in activites:
        reponse = client.get(f"/api/v1/admin/evenements/{evenement}/participation-stats", headers=entete)
        assert reponse.status_code == 200, f"{evenement}: {reponse.status_code}"
        d = reponse.json()
        if d["seuil_evaluation_atteint"]:
            assert d["nb_notes"] >= _SEUIL_EVAL, f"{evenement}: moyenne diffusée avec {d['nb_notes']} note(s)"
        else:
            assert d["note_moyenne"] is None, f"{evenement}: moyenne diffusée sous le seuil"
            assert d["distribution_notes"] == [], f"{evenement}: distribution diffusée sous le seuil"


def test_aucun_taux_publie_ne_depasse_cent_pour_cent() -> None:
    """A share above 100 tells the reader the figure is broken, or worse, is believed.

    Ratings are anonymous and carry no member link, so nothing guarantees a rater was
    counted present. Dividing by the present alone could exceed the whole.
    """
    client, entete = _client_et_entete()
    activites = _activites_evaluees()
    assert activites

    for evenement in activites:
        d = client.get(f"/api/v1/admin/evenements/{evenement}/participation-stats", headers=entete).json()
        for cle, valeur in d.items():
            if cle.startswith("taux_") or cle.startswith("part_"):
                assert isinstance(valeur, (int, float)), f"{evenement}: {cle} n'est pas un nombre"
                assert 0 <= valeur <= 100, f"{evenement}: {cle} = {valeur}"
