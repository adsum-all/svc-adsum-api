"""The published figures must mean what they say, and add up.

Two faults are locked out here, both of which shipped and were found by reading a
dashboard rather than by running a test.

A rate labelled "présence" counted anyone whose status said they had followed, online
included: sixty three people who never came, turning a real 55,0 percent into a
displayed 60,7. Presence is a channel; following is participation. They are different
questions and they get different numbers.

And the same rate was computed in six modules over four denominators, so nothing said
which screen was right. There is now one catalogue, and these tests check it against
the real database.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


def test_l_arithmetique_se_ferme() -> None:
    """Every declared equality holds, on the real data, right now.

    These are the checks published beside the figures. If one of them can fail while the
    dashboard still renders, the dashboard is lying politely.
    """
    from app.indicateurs import calculer

    d = calculer()
    echecs = [c["detail"] for c in d["controles"] if not c["verifie"]]
    assert echecs == [], f"contrôle(s) en échec : {echecs}"
    assert d["coherent"] is True


def test_la_presence_ne_compte_jamais_un_suivi_a_distance() -> None:
    """The defect itself, stated as a property.

    Someone who followed online without coming is a follower, never a presence. The two
    counts must therefore differ by exactly the online population, and presence must
    never exceed following.
    """
    from app.indicateurs import calculer

    b = calculer()["brut"]
    assert b["presentiel"] <= b["suivis"], "la présence physique ne peut dépasser le suivi"
    assert b["presentiel"] + b["en_ligne"] + b["canal_inconnu"] == b["suivis"]
    # And the online population is not empty in this base, so the check is not vacuous.
    assert b["en_ligne"] > 0, "aucun suivi à distance en base : le contrôle serait vide"
    assert b["presentiel"] != b["suivis"], "présence et suivi seraient indiscernables"


def test_un_suivi_partiel_n_est_jamais_une_absence() -> None:
    """The rule the owner stated, checked rather than commented.

    Partial describes an incomplete online follow-up. Counting it as an absence would
    punish someone who did attend, and inflate every absence figure.
    """
    from app.indicateurs import calculer

    b = calculer()["brut"]
    assert b["en_ligne_partiel"] > 0, "aucun suivi partiel en base : le contrôle serait vide"
    # Partial sits inside online, which sits inside following. It can therefore never be
    # part of the absences, and the totals prove it: adding it to absences would break
    # the first equality.
    assert b["en_ligne_partiel"] <= b["en_ligne"]
    assert b["suivis"] + b["absences"] == b["observations"]


def test_aucun_taux_ne_sort_de_ses_bornes() -> None:
    """A share above 100 or below 0 is a broken figure, whatever it is called."""
    from app.indicateurs import calculer

    for t in calculer()["taux"]:
        if t["valeur"] is None:
            continue
        assert 0 <= t["valeur"] <= 100, f"{t['code']} = {t['valeur']}"


def test_un_taux_sur_une_population_vide_n_est_pas_zero() -> None:
    """Undefined and zero are different statements.

    Returning zero for a rate with no population draws a reassuring flat line where
    there is nothing to say, and a reader cannot tell the two apart.
    """
    from app.indicateurs import calculer

    # A filter no row satisfies: every population is empty, so every rate is undefined.
    d = calculer("1 = 0")
    assert d["brut"]["observations"] == 0
    assert all(t["valeur"] is None for t in d["taux"]), "un taux vaut zéro sur une population vide"
    # The equalities still hold: nothing plus nothing is nothing.
    assert d["coherent"] is True


def test_la_synthese_ne_peut_pas_contredire_la_table() -> None:
    """One catalogue, so a card and the audit beside it cannot disagree."""
    from app.direction_pilotage import regles_calcul, synthese

    table = regles_calcul({}, None)["brut"]
    carte = synthese({}, None)

    assert carte["suivis"] == table["suivis"]
    assert carte["absents"] == table["absences"]
    assert carte["presentiel"] == table["presentiel"]
    assert carte["en_ligne"] == table["en_ligne"]
    assert carte["observations"] == table["observations"]
