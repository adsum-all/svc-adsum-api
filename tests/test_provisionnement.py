"""Bringing a client online, and above all what that must refuse to do.

The dangerous step is not creating a database, it is writing into the wrong one. A
connection string pasted with a typo points at a live organisation as easily as at a new
one, and seeding referentials into it mixes two clients in the very place the whole
architecture exists to keep apart.

These tests never create or drop a database: that is slow, and on a pooled server a
freshly opened database refuses to drop for several seconds, which would make the suite
flaky for no gain. They exercise the guards, which is where the risk lives.
"""
from __future__ import annotations

import os

import pytest

from app import provisionnement as pv

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


def test_un_nom_de_base_dangereux_est_refuse() -> None:
    """CREATE DATABASE takes no bound parameter, so the name is interpolated.

    The validation is therefore not cosmetic: it is the only thing standing between a
    caller and arbitrary DDL.
    """
    for mauvais in (
        "Base Majuscule",
        "1commence-par-un-chiffre",
        "avec-des-tirets",
        'guillemet"injection',
        "point.virgule; DROP DATABASE postgres",
        "",
        "ab",
        "x" * 64,
    ):
        with pytest.raises(ValueError):
            pv.valider_nom_base(mauvais)


def test_un_nom_de_base_correct_est_normalise() -> None:
    assert pv.valider_nom_base("  Paroisse_Saint_Jean  ") == "paroisse_saint_jean"


@pytestmark_db
def test_semer_dans_une_base_habitee_est_refuse() -> None:
    """The guard that matters most, exercised against the live database itself.

    If this ever passes, a typo in a connection string writes one organisation's
    referentials into another's data.
    """
    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    resultat = pv.semer_referentiels(dsn)
    assert resultat["fait"] is False
    assert "membres" in str(resultat["motif"]), resultat["motif"]


@pytestmark_db
def test_le_diagnostic_ne_modifie_rien_et_decrit_chaque_etape() -> None:
    """An operator must be able to ask what is missing as often as they like."""
    from app import db

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    avant = db.fetch_one("SELECT count(*) AS n FROM membre", ())
    diagnostic = pv.diagnostiquer(dsn)
    apres = db.fetch_one("SELECT count(*) AS n FROM membre", ())
    assert avant == apres, "le diagnostic doit être en lecture seule"

    codes = [e["code"] for e in diagnostic["etapes"]]
    assert codes == [e.code for e in pv.ETAPES], "chaque étape doit être décrite"
    par_code = {e["code"]: e for e in diagnostic["etapes"]}
    assert par_code["connexion"]["fait"] is True
    # The live database is populated, so this step must read as not satisfied and say so.
    assert par_code["vide"]["fait"] is False
    assert par_code["vide"]["bloquant"] is True
    assert par_code["schema"]["fait"] is True, "la base de production est à la version attendue"


@pytestmark_db
def test_un_diagnostic_sur_une_base_injoignable_ne_leve_pas() -> None:
    """A wrong connection string is an ordinary mistake, not a crash.

    An operator pastes a bad DSN more often than a good one, and a five hundred teaches
    them nothing about what went wrong.
    """
    diagnostic = pv.diagnostiquer("postgresql://personne:rien@hote.invalide:5432/rien")
    par_code = {e["code"]: e for e in diagnostic["etapes"]}
    assert par_code["connexion"]["fait"] is False
    assert par_code["connexion"]["detail"], "la raison de l'échec doit être rendue"
    for suivant in ("vide", "schema", "referentiels"):
        assert par_code[suivant]["fait"] is False


def test_les_identifiants_ne_sont_jamais_recopies() -> None:
    """Seeding a new client with another organisation's API keys would hand over its
    ability to send mail under that organisation's name."""
    assert "integration_config" in pv.JAMAIS_COPIE
    assert "integration_config" not in pv.REFERENTIELS
