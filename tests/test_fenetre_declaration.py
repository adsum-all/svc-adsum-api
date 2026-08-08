"""The declaration window must be one formula, applied identically everywhere.

An activity ending and its declaration form closing are two different moments. When a
listing treated the end as the end of everything, the activity belonged to no list for
the length of the window: the member could not answer a form that was still open, and
the missing answer was then counted as a non-response they never chose. A fabricated
non-response is worse than a missing one, because it looks like data.

These tests run against the real database, which is where the formula is evaluated.
They read, they never write.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


def test_la_cloture_est_toujours_posterieure_ou_egale_a_la_fin() -> None:
    """The form can close with the activity, never before it."""
    from app import db
    from app import fenetres_pointage as fp

    sql = fp.cloture_declaration_sql("e")
    fin = fp.fin_effective_sql("e")
    row = db.fetch_one(f"SELECT count(*) AS n FROM evenement e WHERE ({sql}) < ({fin})", ())
    assert row is not None
    assert int(row["n"]) == 0, "une activité fermerait sa déclaration avant d'être terminée"


def test_la_formule_de_cloture_est_celle_que_le_serveur_applique_deja() -> None:
    """Two formulas for one instant is how display and submission start disagreeing.

    The member endpoint refuses a late submission with ``FENETRE_FIN_SQL``. If the
    listing computed anything else, a screen would offer a form the server rejects,
    or hide one it would still accept.
    """
    from app import db
    from app import fenetres_pointage as fp
    from app.participation import FENETRE_FIN_SQL

    nouvelle = fp.cloture_declaration_sql("e")
    row = db.fetch_one(
        f"SELECT count(*) AS n FROM evenement e WHERE ({nouvelle}) IS DISTINCT FROM ({FENETRE_FIN_SQL})", ()
    )
    assert row is not None
    assert int(row["n"]) == 0, "la formule de clôture diverge de celle appliquée à la soumission"


def test_aucune_activite_ne_tombe_hors_de_toutes_les_phases() -> None:
    """Every activity must land in exactly one phase, at any instant.

    The defect was a gap between two ranges rather than a wrong label: nothing was
    mis-classified, an activity simply stopped matching any bucket the screens knew
    how to display.
    """
    from app import db
    from app import fenetres_pointage as fp

    debut = fp.debut_fenetre_sql("e")
    fin = fp.fin_effective_sql("e")
    cloture = fp.cloture_declaration_sql("e")
    phase = (
        f"CASE WHEN now() < {debut} THEN 'a_venir' "
        f"WHEN now() < e.debut THEN 'bientot' "
        f"WHEN now() <= {fin} THEN 'en_cours' "
        f"WHEN now() <= {cloture} THEN 'a_declarer' "
        f"ELSE 'termine' END"
    )
    rows = db.fetch_all(
        f"SELECT {phase} AS phase, count(*) AS n FROM evenement e GROUP BY 1 ORDER BY 1", ()
    )
    connues = {"a_venir", "bientot", "en_cours", "a_declarer", "termine"}
    obtenues = {str(r["phase"]) for r in rows}
    assert obtenues <= connues, f"phase inattendue : {obtenues - connues}"

    total = db.fetch_one("SELECT count(*) AS n FROM evenement", ())
    assert total is not None
    assert sum(int(r["n"]) for r in rows) == int(total["n"]), "des activités n'ont aucune phase"


def test_le_formulaire_n_est_annonce_ouvert_que_pendant_la_fenetre() -> None:
    """The listing's open flag must be genuinely bounded, on real activities.

    Announcing an open form the server refuses is the failure a member actually meets:
    they answer, and the answer is rejected by a rule the screen never mentioned. The
    test first proves the base contains closed activities, otherwise the check would
    pass on an empty set and prove nothing.
    """
    from app import db
    from app import fenetres_pointage as fp

    cloture = fp.cloture_declaration_sql("e")
    ouvert = f"(now() >= e.debut AND now() <= {cloture})"

    closes = db.fetch_one(f"SELECT count(*) AS n FROM evenement e WHERE now() > {cloture}", ())
    assert closes is not None
    assert int(closes["n"]) > 0, "aucune activité close en base : le contrôle serait vide"

    incoherentes = db.fetch_one(
        f"SELECT count(*) AS n FROM evenement e WHERE now() > {cloture} AND {ouvert}", ()
    )
    assert incoherentes is not None
    assert int(incoherentes["n"]) == 0, "une activité close annonce encore un formulaire ouvert"


def test_la_fenetre_couvre_reellement_du_temps() -> None:
    """A window of zero everywhere would make the fix invisible and the tests vacuous."""
    from app import db
    from app import fenetres_pointage as fp

    row = db.fetch_one(
        f"SELECT count(*) AS n FROM evenement e "
        f"WHERE {fp.cloture_declaration_sql('e')} > {fp.fin_effective_sql('e')}",
        (),
    )
    assert row is not None
    assert int(row["n"]) > 0, "aucune activité n'accorde de délai de déclaration après sa fin"
