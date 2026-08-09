"""A module that has not been subscribed must be refused, not merely hidden.

Hiding a button is decoration: the endpoints stay reachable to anyone who types the
address, and an organisation that dropped a module from its contract would keep using it
exactly as before. The platform would be sold on the honour system.

The licence rows are created and deleted inside the test, so the live organisation never
keeps a control subscription that would take its own modules offline.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)

#: Endpoints that belong to a module, one per module, used to prove the refusal reaches
#: the routes rather than only the helper.
ROUTES = {
    "direction": "/api/v1/direction/synthese",
    "pilotage": "/api/v1/pilotage/moi",
    "collaboration": "/api/v1/collaboration/espaces",
}


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


def _connexion():
    import psycopg

    return psycopg.connect(os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1))


def test_sans_abonnement_declare_tout_le_catalogue_est_servi() -> None:
    """The transition state, which is what production is in.

    Reading "no row" as "no module" would have taken the platform offline the moment the
    table shipped, since the organisation running today predates it.
    """
    from app import modules_souscrits as ms

    assert ms.souscriptions() == set()
    for code in ROUTES:
        assert ms.souscrit(code), code

    client, entete = _contexte()
    for code, route in ROUTES.items():
        assert client.get(route, headers=entete).status_code == 200, code


def test_un_module_hors_abonnement_est_refuse_sur_ses_routes() -> None:
    """The rule itself, exercised end to end rather than on the helper alone."""
    from app import db

    client, entete = _contexte()
    organisation = db.fetch_one("SELECT id FROM organisation_cliente LIMIT 1", ())
    if organisation is None:
        pytest.skip("aucune organisation cliente enregistrée")

    with _connexion() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO licence (organisation_id, formule, debut) "
                "VALUES (%s, 'controle-modules', current_date) RETURNING id",
                (organisation["id"],),
            )
            ligne = cur.fetchone()
            assert ligne is not None
            licence = ligne[0]
            # Only the back office is subscribed.
            cur.execute(
                "INSERT INTO licence_module (licence_id, application_code) VALUES (%s, 'back-office')",
                (licence,),
            )
            conn.commit()
        try:
            for code, route in ROUTES.items():
                reponse = client.get(route, headers=entete)
                assert reponse.status_code == 402, f"{code} devrait être refusé, reçu {reponse.status_code}"
                # 402 rather than 403: this account may well be entitled, the
                # organisation simply has not bought the module, and only that
                # distinction tells the reader who to talk to.
                assert code in reponse.json()["detail"]

            # And a subscribed surface keeps working: the guard must not be a blanket.
            assert client.get("/api/v1/admin/participation/global", headers=entete).status_code == 200
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM licence WHERE id = %s", (licence,))
                conn.commit()


def test_un_module_inconnu_ne_peut_pas_etre_souscrit() -> None:
    """A typo in a contract must fail at once, not silently sell nothing."""
    from fastapi import HTTPException

    from app import db
    from app import modules_souscrits as ms

    organisation = db.fetch_one("SELECT id FROM organisation_cliente LIMIT 1", ())
    if organisation is None:
        pytest.skip("aucune organisation cliente enregistrée")

    with _connexion() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO licence (organisation_id, formule, debut) "
                "VALUES (%s, 'controle-inconnu', current_date) RETURNING id",
                (organisation["id"],),
            )
            ligne = cur.fetchone()
            assert ligne is not None
            licence = str(ligne[0])
            conn.commit()
        try:
            with pytest.raises(HTTPException) as refus:
                ms.definir_modules(licence, ["back-office", "module-qui-n-existe-pas"])
            assert refus.value.status_code == 400
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM licence WHERE id = %s", (licence,))
                conn.commit()


def test_le_catalogue_dit_ce_qui_est_souscrit() -> None:
    """The console shows the contract, so the catalogue must carry that state."""
    from app import modules_souscrits as ms

    catalogue = ms.catalogue()
    assert catalogue, "le catalogue des modules est vide"
    assert {"code", "nom", "souscrit", "actif"} <= set(catalogue[0])
    # In transition every module reads as subscribed, which is the documented meaning.
    assert all(m["souscrit"] for m in catalogue)
