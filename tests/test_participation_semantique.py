"""The participation vocabulary must mean exactly one thing.

Three words were doing four jobs. "Présent" covered a scan at the door and a member
typing into a form; "partiel" was offered for on-site attendance, where it means
nothing; and an absence carried no reason, so an excused absence and a silent one were
the same row. Statistics built on that are exact arithmetic on false categories.

These tests pin the rules the organisation actually stated, on the real database and
through the real HTTP surface. Each one is a rule somebody can read out loud:

  a scan proves presence and nothing may contradict it;
  partial attendance exists online and nowhere else;
  a member gives a reason, a responsible person decides whether it excuses.

The fixture creates a throwaway activity that started half an hour ago, because the
declaration window closes shortly after an activity ends, and that guard is itself one
of the rules. Everything written is removed afterwards.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.security import create_access_token


def _base_joignable() -> bool:
    try:
        db.fetch_one("SELECT 1 AS ok", ())
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _base_joignable(), reason="base de données non joignable dans cet environnement"
)


@pytest.fixture
def terrain():
    """A throwaway activity, an open window, and a demonstration member to answer."""
    ev = db.fetch_one(
        "INSERT INTO evenement (titre, debut, fin, type, volet) "
        "VALUES (%s, now() - interval '30 minutes', now() - interval '5 minutes', "
        "'reunion', 'A') RETURNING id",
        ("TEST participation sémantique",),
    )
    u = db.fetch_one(
        "SELECT u.id, u.role, u.membre_id FROM utilisateur u JOIN membre m ON m.id = u.membre_id "
        "WHERE u.actif AND m.nom = 'DÉMO' AND m.statut = 'actif' LIMIT 1",
        (),
    )
    if not (ev and u):
        pytest.skip("aucun membre de démonstration avec compte")
    eid, mid = str(ev["id"]), str(u["membre_id"])
    jeton = create_access_token(str(u["id"]), str(u["role"]))
    yield {
        "client": TestClient(app),
        "url": f"/api/v1/membres/me/evenements/{eid}/participation",
        "entetes": {"Authorization": f"Bearer {jeton}"},
        "evenement_id": eid,
        "membre_id": mid,
    }
    db.execute("DELETE FROM participation WHERE evenement_id = %s", (eid,))
    db.execute("DELETE FROM evenement WHERE id = %s", (eid,))


def _declarer(t, corps):
    """Answer the form from a clean slate: a finalised row is deliberately immutable."""
    db.execute(
        "DELETE FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (t["evenement_id"], t["membre_id"]),
    )
    return t["client"].put(t["url"], headers=t["entetes"], json=corps)


def _ligne(t):
    return db.fetch_one(
        "SELECT statut, source, mode_suivi, niveau_en_ligne, confiance, absence_motif, "
        "absence_qualification, qualifie_par FROM participation "
        "WHERE evenement_id = %s AND membre_id = %s",
        (t["evenement_id"], t["membre_id"]),
    )


# --- Partial belongs to online attendance, and nowhere else -----------------

def test_le_partiel_en_presentiel_est_refuse(terrain):
    """On site you were there or you were not.

    This one combination produced 109 rows in production that nobody can interpret:
    they may mean the person arrived late, or followed intermittently from a phone.
    Refusing it is the only way to stop making more.
    """
    r = _declarer(terrain, {
        "a_suivi": True, "mode_suivi": "presentiel", "niveau_en_ligne": "partiel", "valider": True,
    })
    assert r.status_code == 400
    assert "en ligne" in r.json()["detail"].lower()


def test_un_ancien_client_ne_peut_plus_enregistrer_partiel_en_presentiel(terrain):
    """The applications already installed keep working, except for this combination.

    Accepting the old flat pair is what lets the deployed versions carry on. Accepting
    this particular pair would keep producing the rows the whole change exists to stop.
    """
    r = _declarer(terrain, {"statut": "partiel", "modalite": "presentiel", "valider": True})
    assert r.status_code == 400


def test_le_partiel_en_ligne_est_accepte(terrain):
    r = _declarer(terrain, {
        "a_suivi": True, "mode_suivi": "en_ligne", "niveau_en_ligne": "partiel", "valider": True,
    })
    assert r.status_code == 200
    ligne = _ligne(terrain)
    assert ligne["mode_suivi"] == "en_ligne"
    assert ligne["niveau_en_ligne"] == "partiel"


# --- The three questions ----------------------------------------------------

def test_les_trois_reponses_possibles_sont_enregistrees_distinctement(terrain):
    """Followed on site, followed online in full, did not follow: three distinct rows."""
    cas = [
        ({"a_suivi": True, "mode_suivi": "presentiel"}, "present", "presentiel", None),
        ({"a_suivi": True, "mode_suivi": "en_ligne", "niveau_en_ligne": "complet"},
         "present", "en_ligne", "complet"),
        ({"a_suivi": False}, "absent", "aucun", None),
    ]
    for corps, statut, mode, niveau in cas:
        r = _declarer(terrain, {**corps, "valider": True})
        assert r.status_code == 200, r.json()
        ligne = _ligne(terrain)
        assert ligne["statut"] == statut
        assert ligne["mode_suivi"] == mode
        assert ligne["niveau_en_ligne"] == niveau


def test_une_declaration_n_est_jamais_une_preuve(terrain):
    """What the member typed is recorded as their word, not as evidence.

    Without this, a dashboard adds a presence somebody proved at the door to one
    somebody asserted, and reports the sum as attendance.
    """
    _declarer(terrain, {"a_suivi": True, "mode_suivi": "presentiel", "valider": True})
    assert _ligne(terrain)["confiance"] == "declaree"


# --- The reason is not the decision -----------------------------------------

def test_un_motif_hors_catalogue_est_refuse(terrain):
    r = _declarer(terrain, {"a_suivi": False, "absence_motif": "motif_invente", "valider": True})
    assert r.status_code == 400


def test_un_motif_qui_exige_une_precision_l_exige(terrain):
    """« Autre raison » without a word is not a reason."""
    r = _declarer(terrain, {"a_suivi": False, "absence_motif": "autre", "valider": True})
    assert r.status_code == 400
    r = _declarer(terrain, {
        "a_suivi": False, "absence_motif": "autre",
        "absence_commentaire": "Déplacement imprévu", "valider": True,
    })
    assert r.status_code == 200


def test_le_membre_ne_peut_pas_excuser_sa_propre_absence(terrain):
    """A reason given opens a decision; it never is one.

    The row leaves the member's hands awaiting review, with no decider recorded. Any
    other outcome would let somebody excuse themselves, which is the single rule the
    organisation was most explicit about.
    """
    r = _declarer(terrain, {"a_suivi": False, "absence_motif": "sante", "valider": True})
    assert r.status_code == 200
    ligne = _ligne(terrain)
    assert ligne["absence_qualification"] == "en_attente"
    assert ligne["qualifie_par"] is None


def test_une_absence_sans_motif_n_attend_aucune_decision(terrain):
    """Not giving a reason is a valid answer, and it does not queue for review."""
    r = _declarer(terrain, {"a_suivi": False, "valider": True})
    assert r.status_code == 200
    assert _ligne(terrain)["absence_qualification"] == "sans_objet"


# --- The scan is the strongest fact -----------------------------------------

def test_un_membre_scanne_ne_peut_pas_se_declarer_absent(terrain):
    """The controller saw this person. No form may say otherwise."""
    db.execute(
        "DELETE FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (terrain["evenement_id"], terrain["membre_id"]),
    )
    db.execute(
        "INSERT INTO participation (evenement_id, membre_id, statut, source, valide, modalite, "
        "mode_suivi, confiance) VALUES (%s, %s, 'present', 'scan', true, 'presentiel', "
        "'presentiel', 'prouvee')",
        (terrain["evenement_id"], terrain["membre_id"]),
    )
    terrain["client"].put(
        terrain["url"], headers=terrain["entetes"],
        json={"a_suivi": False, "absence_motif": "sante", "valider": True},
    )
    ligne = _ligne(terrain)
    assert ligne["source"] == "scan"
    assert ligne["statut"] == "present"
    assert ligne["confiance"] == "prouvee"


def test_le_catalogue_des_motifs_est_servi(terrain):
    r = terrain["client"].get("/api/v1/reference/motifs-absence", headers=terrain["entetes"])
    assert r.status_code == 200
    motifs = r.json()
    assert len(motifs) >= 5
    codes = {m["code"] for m in motifs}
    assert "sante" in codes
    # The one reason that demands an explanation says so, rather than leaving each
    # application to hard-code the exception.
    assert next(m for m in motifs if m["code"] == "autre")["commentaire_requis"] is True
