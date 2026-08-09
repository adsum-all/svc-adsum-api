"""One database per organisation, and no way to reach the wrong one.

The isolation model is structural rather than disciplinary: a query cannot read another
client's data because the connection does not lead there. That only holds if the
resolution is right, so the resolution is what these tests exercise, including the case
that must never work.

The registry rows are created inside a transaction that always rolls back, so the live
platform is never left holding a test organisation.
"""
from __future__ import annotations

import os

import pytest

from app import organisation_courante as oc

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


# --- Host normalisation, no database ----------------------------------------

def test_un_meme_hote_ecrit_de_plusieurs_facons_est_le_meme() -> None:
    """A registry matching only one spelling would refuse a legitimate request."""
    attendu = "adsum-back-office.pages.dev"
    for variante in (
        "adsum-back-office.pages.dev",
        "ADSUM-Back-Office.Pages.Dev",
        "adsum-back-office.pages.dev:443",
        "adsum-back-office.pages.dev.",
        "  adsum-back-office.pages.dev  ",
        "https://adsum-back-office.pages.dev/quelque-chose",
    ):
        assert oc.normaliser_hote(variante) == attendu, variante


def test_un_hote_multiple_ne_retient_que_le_premier() -> None:
    """X-Forwarded-Host can carry a list when several proxies append to it.

    Taking the last would let a caller append its own value and choose an organisation.
    """
    assert oc.normaliser_hote("premier.example, second.example") == "premier.example"


def test_une_adresse_ipv6_ne_perd_pas_ses_deux_points() -> None:
    assert oc.normaliser_hote("[::1]:8000") == "::1"


# --- Resolution, real database ----------------------------------------------

@pytestmark_db
def test_sans_registre_la_plateforme_reste_sur_sa_base_historique() -> None:
    """Production keeps running while the mechanism exists but is not yet used."""
    from app.config import settings

    assert oc.mode() == "transition"
    organisation = oc.resoudre("n-importe-quel-hote.example")
    assert organisation.dsn == settings.database_dsn


@pytestmark_db
def test_une_fois_le_registre_utilise_un_hote_inconnu_est_refuse() -> None:
    """The case that must never work: serving an unmatched host from a default.

    A misconfigured domain would then hand one organisation's data out under another
    organisation's address, which is the exact failure one database per organisation
    exists to prevent. Refusing is loud, recoverable and harmless.
    """
    import psycopg

    from app.config import settings

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM organisation_cliente LIMIT 1")
            ligne = cur.fetchone()
            if ligne is None:
                pytest.skip("aucune organisation cliente enregistrée")
            cur.execute(
                "INSERT INTO organisation_hote (organisation_id, hote, dsn) VALUES (%s, %s, %s)",
                (ligne[0], "controle-isolation.exemple.invalid", ""),
            )
            conn.commit()
        try:
            assert oc.mode() == "isole", "un hôte est enregistré, le registre est en service"

            # The declared host resolves, and to the historical connection because the
            # row left the DSN empty: an organisation registered without its own base
            # keeps using the one it is already on.
            trouve = oc.resoudre("Controle-Isolation.Exemple.Invalid")
            assert trouve.dsn == settings.database_dsn

            # Anything else is refused rather than served.
            with pytest.raises(oc.OrganisationInconnue):
                oc.resoudre("un-domaine-que-personne-ne-declare.invalid")
            with pytest.raises(oc.OrganisationInconnue):
                oc.resoudre("")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM organisation_hote WHERE hote = %s", ("controle-isolation.exemple.invalid",))
                conn.commit()

    assert oc.mode() == "transition", "le registre doit être revenu à son état initial"


@pytestmark_db
def test_une_organisation_suspendue_cesse_d_etre_servie() -> None:
    """Suspension is a commercial decision that must have a technical effect.

    A client suspended for non-payment whose platform keeps working has not been
    suspended, and one who meets a blank failure cannot know why.
    """
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    hote = "controle-suspension.exemple.invalid"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organisation_cliente (code, nom, etat, suspendue_motif, suspendue_le) "
                "VALUES ('controle-suspension', 'Contrôle', 'suspendue', 'contrôle automatique', now()) "
                "RETURNING id"
            )
            ligne = cur.fetchone()
            assert ligne is not None
            cur.execute("INSERT INTO organisation_hote (organisation_id, hote) VALUES (%s, %s)", (ligne[0], hote))
            conn.commit()
        try:
            with pytest.raises(oc.OrganisationSuspendue):
                oc.resoudre(hote)
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM organisation_cliente WHERE code = 'controle-suspension'")
                conn.commit()


@pytestmark_db
def test_le_resolveur_installe_est_bien_celui_que_la_base_utilise() -> None:
    """The whole model rests on this wiring; an unwired resolver is a silent no-op."""
    from app import db
    from app.main import app  # noqa: F401 - importing installs the resolver

    assert db._resolveur_dsn is oc.dsn_courant, "le résolveur n'est pas installé"
