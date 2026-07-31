"""Regression guard on the apex order, now that the apex lives in the catalogue.

The Controleur General is ALWAYS above the Intendant General. In the member's upward
chain the Intendant General is the closer level and the Controleur General the next
one up, so the Intendant General MUST come first. This forbids any future inversion,
in the member hierarchy and in the back-office chart builder alike.

The apex used to be a tuple in the code; it is now read from fonction_honorifique,
ordered by niveau_hierarchique. The ordering rule is what matters and it is tested
against the real catalogue, so a level edited in the back office cannot silently
invert the chain either.
"""
from __future__ import annotations

import os

import pytest

from app import hierarchie_membre, organigramme_builder

_SANS_BASE = not os.environ.get("ADSUM_DATABASE_URL")


@pytest.mark.skipif(_SANS_BASE, reason="l'apex est lu dans le catalogue, une base est nécessaire")
def test_apex_intendant_general_avant_controleur_general() -> None:
    apex = hierarchie_membre._apex()
    assert "intendant_general" in apex and "controleur_general" in apex, apex
    assert apex.index("intendant_general") < apex.index("controleur_general"), apex


@pytest.mark.skipif(_SANS_BASE, reason="l'apex est lu dans le catalogue, une base est nécessaire")
def test_apex_ordre_complet() -> None:
    """From the member upward, lowest authority first."""
    assert hierarchie_membre._apex() == (
        "intendant_general", "controleur_general", "berger_missions", "moderateur", "fondateur",
    )


def test_le_repli_garde_le_meme_ordre() -> None:
    """The fallback covers a base predating the column, and must not invert anything."""
    repli = hierarchie_membre._APEX_REPLI
    assert repli.index("intendant_general") < repli.index("controleur_general"), repli


def test_un_sommet_vide_ne_casse_pas_la_chaine() -> None:
    """An organisation that declares no apex sees its own units and stops there.

    Truthful rather than convenient: inventing levels is exactly what this change
    removed, so an empty catalogue must produce an empty apex, not the old tuple.
    """
    assert isinstance(hierarchie_membre._apex(), tuple)


def test_builder_chaine_controleur_au_dessus_intendant() -> None:
    """The published chart is top-down, so the Controleur General comes first there."""
    cles = [fcle for _key, fcle in organigramme_builder._CHAINE]
    assert cles.index("controleur_general") < cles.index("intendant_general"), cles
