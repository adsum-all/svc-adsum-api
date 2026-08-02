"""The platform must be able to belong to an organisation that is not this one.

This product is meant to be sold to any association, parish or church, each with its
own name, colours and words. That promise is only kept if nothing about the current
organisation is written into the code, and the one route that carries the identity to
every screen before anybody signs in is /api/v1/marque.

These tests drive that route with another organisation's settings and check that what
comes out is entirely theirs. They also cover the two ways a settings row can reach a
page and do harm: a colour lands inside a style attribute, and an address lands inside
an href on a page served without authentication.

No database and no network: the settings read is replaced, so this runs in CI and
cannot alter what a real organisation is currently showing its members.
"""
from __future__ import annotations

import pytest

from app import marque as module_marque
from app import marque_publique

#: What a different client would have configured. Nothing here shares a character
#: with the organisation running today, so a value leaking through is unmistakable.
_PAROISSE = {
    "org_marque": "PAROISSE",
    "org_nom": "Paroisse Saint-Pierre",
    "org_nom_court": "PSP",
    "org_ville": "Lyon",
    "org_signature": "La Paroisse",
    "org_couleur_principale": "#7a1f3d",
    "org_couleur_sombre": "#4d1326",
    "org_baseline": "communauté paroissiale",
    "org_slogan": "Ensemble depuis 1892",
    "org_logo_url": "https://saint-pierre.example/logo.png",
    "org_site": "https://saint-pierre.example",
    "org_url_membre": "https://membres.saint-pierre.example",
    "org_url_back_office": "https://admin.saint-pierre.example",
    "org_url_public": "https://saint-pierre.example/accueil",
}


@pytest.fixture
def _reglages(monkeypatch):
    """Serve the given settings to both modules that read them."""

    def poser(valeurs: dict[str, str]) -> None:
        def fetch_all(sql, params, role=None, **kwargs):
            demandees = params[0] if params else []
            return [{"cle": c, "valeur": valeurs[c]} for c in demandees if c in valeurs]

        monkeypatch.setattr(module_marque.db, "fetch_all", fetch_all)
        monkeypatch.setattr(marque_publique.db, "fetch_all", fetch_all)
        monkeypatch.setattr(marque_publique, "vocabulaire_rendu", lambda: {
            "tribu": {
                "singulier": "secteur", "pluriel": "secteurs", "article": "le",
                "Singulier": "Secteur", "Pluriel": "Secteurs", "avec_article": "le secteur",
            },
        })

    return poser


def test_the_route_carries_the_configured_organisation_and_nothing_of_this_one(_reglages):
    _reglages(_PAROISSE)
    servie = marque_publique.marque_publique()

    assert servie["marque"] == "PAROISSE"
    assert servie["organisation"] == "Paroisse Saint-Pierre"
    assert servie["organisation_courte"] == "PSP"
    assert servie["slogan"] == "Ensemble depuis 1892"
    assert servie["couleur"] == "#7a1f3d"
    assert servie["couleur_sombre"] == "#4d1326"
    # The monogram follows the brand rather than being a stored letter, so it can
    # never disagree with the name printed next to it.
    assert servie["initiale"] == "P"
    # The words the organisation chose, not the ones this one uses.
    assert servie["mots"]["tribu"]["pluriel"] == "secteurs"

    # The decisive assertion: nothing of the organisation running today survives
    # anywhere in the payload, at any depth.
    rendu = str(servie)
    for trace in ("Sacerdoce", "ADSUM", "Abidjan", "2a4fad", "1d3470", "sacerdoceroyal"):
        assert trace not in rendu, f"identité de l'organisation actuelle retrouvée : {trace}"

    # "tribu" survives as the KEY a screen looks a word up by, which is part of the
    # contract and must not change. What must be the parish's is the word it maps to.
    for facette in ("singulier", "pluriel", "Singulier", "Pluriel", "avec_article"):
        assert "tribu" not in servie["mots"]["tribu"][facette].lower()


def test_the_application_addresses_are_served_so_no_front_has_to_hardcode_one(_reglages):
    _reglages(_PAROISSE)
    servie = marque_publique.marque_publique()
    assert servie["url_membre"] == "https://membres.saint-pierre.example"
    assert servie["url_back_office"] == "https://admin.saint-pierre.example"
    assert servie["url_public"] == "https://saint-pierre.example/accueil"


def test_an_unconfigured_address_is_null_rather_than_another_organisation_s(_reglages):
    """Null means "offer no link". A fallback would send members to this platform."""
    _reglages({k: v for k, v in _PAROISSE.items() if not k.startswith("org_url_")})
    servie = marque_publique.marque_publique()
    assert servie["url_membre"] is None
    assert servie["url_back_office"] is None
    assert servie["url_public"] is None


@pytest.mark.parametrize("hostile", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "  javascript:alert(1)",
    "https://ok.example /x",          # a space would break out of the attribute
])
def test_an_address_that_is_not_plainly_http_never_reaches_an_href(_reglages, hostile):
    """This route is unauthenticated, so its output lands on the sign-in page.

    A settings row holding a script URL would turn that page into a place where
    clicking a link runs whatever the last administrator typed.
    """
    _reglages({**_PAROISSE, "org_url_back_office": hostile, "org_site": hostile})
    servie = marque_publique.marque_publique()
    assert servie["url_back_office"] is None
    assert servie["site"] is None


@pytest.mark.parametrize("valide", [
    "https://membres.example.org",
    "http://interne.example.org:8080/portail",
])
def test_a_plain_http_address_is_served_unchanged(_reglages, valide):
    _reglages({**_PAROISSE, "org_url_membre": valide})
    assert marque_publique.marque_publique()["url_membre"] == valide


@pytest.mark.parametrize("hostile", [
    "red; background: url(https://ailleurs.example/pixel)",
    "</style><script>alert(1)</script>",
    "expression(alert(1))",
    "#12345",       # not a length a hex colour ever has
    "",
])
def test_a_colour_that_is_not_a_hex_falls_back_instead_of_entering_a_style(_reglages, hostile):
    """The colour is concatenated into a style attribute in every message sent."""
    _reglages({**_PAROISSE, "org_couleur_principale": hostile})
    servie = marque_publique.marque_publique()
    assert servie["couleur"] == "#2a4fad"          # the shipped colour, not the input
    assert "alert" not in servie["couleur"]
    assert ";" not in servie["couleur"]


@pytest.mark.parametrize("court", ["#abc", "#ABC"])
def test_a_three_digit_hex_is_accepted_because_it_is_a_real_colour(_reglages, court):
    _reglages({**_PAROISSE, "org_couleur_principale": court})
    assert marque_publique.marque_publique()["couleur"] == court


def test_an_empty_settings_table_still_renders_a_coherent_platform(_reglages):
    """A fresh deployment has configured nothing. The sign-in screen must still work."""
    _reglages({})
    servie = marque_publique.marque_publique()
    assert servie["marque"]
    assert servie["initiale"]
    assert servie["couleur"].startswith("#")
    assert servie["url_back_office"] is None


def test_a_database_failure_does_not_take_the_sign_in_screen_down(monkeypatch):
    """Everything on this route is public and has a shipped default, so an outage
    on the settings table must degrade the branding, never the ability to sign in."""

    def explose(*args, **kwargs):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(module_marque.db, "fetch_all", explose)
    monkeypatch.setattr(marque_publique.db, "fetch_all", explose)
    monkeypatch.setattr(marque_publique, "vocabulaire_rendu", dict)

    servie = marque_publique.marque_publique()
    assert servie["marque"]
    assert servie["couleur"].startswith("#")
