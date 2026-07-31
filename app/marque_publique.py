"""The organisation's identity, readable before anyone signs in.

The sign-in screen and the application header carry a name and a palette, and they are
shown to somebody who has no token yet. Serving them from an authenticated route was
therefore impossible, so they were written into the front-end code: every deployment
of this platform would greet its members with somebody else's name.

This route answers without authentication, on purpose, and returns only what is
already on the public face of the application: how the organisation calls itself and
what colours it uses. Nothing here is private, and anybody who can reach the sign-in
page can already read all of it off the page.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from . import db
from .marque import marque
from .vocabulaire import rendu as vocabulaire_rendu

router = APIRouter(prefix="/api/v1", tags=["reference"])

# What the front may show before a session exists. Deliberately narrow: anything not
# on this list stays behind authentication, whatever the settings table holds.
_PUBLIC = ("org_nom", "org_nom_court", "org_slogan", "org_logo_url", "org_site")


@router.get("/marque")
def marque_publique() -> dict[str, Any]:
    """Name, tagline, logo and palette, for the screens shown before signing in."""
    m = marque()
    valeurs: dict[str, str] = {}
    try:
        lignes = db.fetch_all(
            "SELECT cle, valeur FROM integration_config WHERE cle = ANY(%s)", (list(_PUBLIC),)
        )
        valeurs = {str(r["cle"]): (r.get("valeur") or "").strip() for r in lignes}
    except Exception:  # noqa: BLE001 - the sign-in screen must render even without the base
        valeurs = {}

    return {
        # The large line in the header and on the sign-in screen.
        "marque": m.marque,
        "initiale": m.initiale,
        # Who is writing: shown under the brand, and in the footer.
        "organisation": valeurs.get("org_nom") or m.nom,
        "organisation_courte": valeurs.get("org_nom_court") or m.nom_court,
        "slogan": valeurs.get("org_slogan") or None,
        "logo_url": valeurs.get("org_logo_url") or None,
        "site": valeurs.get("org_site") or None,
        "couleur": m.couleur,
        "couleur_sombre": m.couleur_sombre,
        # How this organisation names its own units and responsibilities. Shipped
        # with the identity because a screen needs both at once, and a second call
        # would show the right colours around the wrong words.
        "mots": vocabulaire_rendu(),
    }
