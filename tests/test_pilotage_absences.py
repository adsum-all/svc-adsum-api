"""Only a responsible person excuses an absence, and the decision leaves a trace.

The organisation was more explicit about this than about anything else: a member may
say why they were not there, and somebody else decides what that is worth. Until this
module existed the rule was unbreakable only because it was unimplementable, and a
rule nobody can apply is not a rule.

These tests pin the three things that make it real. The decision is bounded to the
caller's perimeter. It records who took it and when, enforced by the schema rather
than by every write path remembering. And returning a file to review clears the
decider, because a pending absence still carrying a name reads as decided.
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
    """An activity, a demonstration member who declared an absence, and a decider."""
    ev = db.fetch_one(
        "INSERT INTO evenement (titre, debut, fin, type, volet) "
        "VALUES (%s, now() - interval '2 hours', now() - interval '1 hour', 'reunion', 'A') "
        "RETURNING id",
        ("TEST qualification d'absence",),
    )
    membre = db.fetch_one(
        "SELECT id FROM membre WHERE nom = 'DÉMO' AND statut = 'actif' LIMIT 1", ()
    )
    # A globally scoped account, so the perimeter is not what the test is measuring.
    decideur = db.fetch_one(
        "SELECT id, role FROM utilisateur WHERE role IN ('admin', 'super_admin') AND actif LIMIT 1", ()
    )
    if not (ev and membre and decideur):
        pytest.skip("jeu de données insuffisant")

    eid, mid = str(ev["id"]), str(membre["id"])
    db.execute(
        "INSERT INTO participation (evenement_id, membre_id, statut, source, valide, "
        "mode_suivi, confiance, absence_motif, absence_commentaire, absence_qualification) "
        "VALUES (%s, %s, 'absent', 'declaration', true, 'aucun', 'declaree', "
        "'sante', 'Grippe', 'en_attente')",
        (eid, mid),
    )
    yield {
        "client": TestClient(app),
        "entetes": {"Authorization": "Bearer " + create_access_token(str(decideur["id"]), str(decideur["role"]))},
        "evenement_id": eid,
        "membre_id": mid,
        "decideur_id": str(decideur["id"]),
    }
    db.execute("DELETE FROM participation WHERE evenement_id = %s", (eid,))
    db.execute("DELETE FROM evenement WHERE id = %s", (eid,))


def _ligne(t):
    return db.fetch_one(
        "SELECT absence_qualification, qualifie_par, qualifie_le, qualification_commentaire "
        "FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (t["evenement_id"], t["membre_id"]),
    )


def test_l_absence_declaree_apparait_en_attente(terrain):
    """A reason given puts the file in front of somebody, which is its whole purpose."""
    r = terrain["client"].get(
        "/api/v1/pilotage/absences?qualification=en_attente&limite=100",
        headers=terrain["entetes"],
    )
    assert r.status_code == 200
    corps = r.json()
    trouvee = [
        a for a in corps["absences"]
        if a["evenement_id"] == terrain["evenement_id"] and a["membre_id"] == terrain["membre_id"]
    ]
    assert trouvee, "l'absence déclarée n'apparaît pas dans la file de décision"
    assert trouvee[0]["motif"] == "sante"
    assert trouvee[0]["motif_libelle"] == "Problème de santé"
    assert trouvee[0]["decideur"] is None


def test_excuser_une_absence_enregistre_qui_a_decide_et_quand(terrain):
    """A decision without a decider is not a decision, and the schema refuses one."""
    r = terrain["client"].put(
        f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}",
        headers=terrain["entetes"],
        json={"qualification": "excusee", "commentaire": "Certificat reçu"},
    )
    assert r.status_code == 200, r.json()
    ligne = _ligne(terrain)
    assert ligne["absence_qualification"] == "excusee"
    assert str(ligne["qualifie_par"]) == terrain["decideur_id"]
    assert ligne["qualifie_le"] is not None
    assert ligne["qualification_commentaire"] == "Certificat reçu"


def test_refuser_une_absence_est_aussi_une_decision_tracee(terrain):
    r = terrain["client"].put(
        f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}",
        headers=terrain["entetes"],
        json={"qualification": "non_excusee", "commentaire": "Sans justificatif"},
    )
    assert r.status_code == 200
    ligne = _ligne(terrain)
    assert ligne["absence_qualification"] == "non_excusee"
    assert ligne["qualifie_par"] is not None


def test_rouvrir_un_dossier_efface_le_decideur(terrain):
    """Sending a file back to review must not leave it looking decided.

    A pending absence still carrying somebody's name would be read as their decision,
    and the person named never took it back.
    """
    client, entetes = terrain["client"], terrain["entetes"]
    url = f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}"
    client.put(url, headers=entetes, json={"qualification": "excusee"})
    assert _ligne(terrain)["qualifie_par"] is not None

    r = client.put(url, headers=entetes, json={"qualification": "en_attente", "commentaire": "À revoir"})
    assert r.status_code == 200
    ligne = _ligne(terrain)
    assert ligne["absence_qualification"] == "en_attente"
    assert ligne["qualifie_par"] is None
    assert ligne["qualifie_le"] is None


def test_une_decision_inconnue_est_refusee(terrain):
    r = terrain["client"].put(
        f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}",
        headers=terrain["entetes"],
        json={"qualification": "approuvee_par_le_membre"},
    )
    assert r.status_code == 400


def test_on_ne_qualifie_pas_une_participation_qui_n_est_pas_une_absence(terrain):
    """Somebody who followed the activity has nothing to excuse."""
    db.execute(
        "UPDATE participation SET statut = 'present', mode_suivi = 'presentiel', "
        "absence_qualification = 'sans_objet', absence_motif = NULL "
        "WHERE evenement_id = %s AND membre_id = %s",
        (terrain["evenement_id"], terrain["membre_id"]),
    )
    r = terrain["client"].put(
        f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}",
        headers=terrain["entetes"],
        json={"qualification": "excusee"},
    )
    assert r.status_code == 409


def test_la_synthese_donne_les_nombres_et_le_taux(terrain):
    """A percentage without the count behind it lets three people read as three hundred."""
    r = terrain["client"].get("/api/v1/pilotage/absences/synthese", headers=terrain["entetes"])
    assert r.status_code == 200
    s = r.json()
    for cle in ("en_attente", "excusees", "non_excusees", "absences_totales", "taux_excusees", "par_motif"):
        assert cle in s
    assert s["en_attente"] >= 1
    assert isinstance(s["par_motif"], list)


def test_la_decision_est_journalisee(terrain):
    """A decision contested months later has to be reconstructable."""
    avant = db.fetch_one(
        "SELECT count(*) AS n FROM audit WHERE action = 'qualification_absence'", ()
    )
    terrain["client"].put(
        f"/api/v1/pilotage/absences/{terrain['evenement_id']}/{terrain['membre_id']}",
        headers=terrain["entetes"],
        json={"qualification": "excusee", "commentaire": "Justifié"},
    )
    apres = db.fetch_one(
        "SELECT count(*) AS n FROM audit WHERE action = 'qualification_absence'", ()
    )
    assert int(apres["n"]) == int(avant["n"]) + 1
