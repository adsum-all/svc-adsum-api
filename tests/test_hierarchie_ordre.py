"""Regression guard on the apex hierarchy order: the Controleur General is ALWAYS
above the Intendant General. In the member's upward chain the Intendant General is
the closer (lower N) apex level and the Controleur General the next one up, so in the
apex tuple intendant_general MUST come before controleur_general. This test forbids
any future inversion (both in the member hierarchy and the back-office chart builder)."""
from __future__ import annotations

from app import hierarchie_membre, organigramme_builder


def test_apex_intendant_general_avant_controleur_general() -> None:
    apex = hierarchie_membre._APEX
    assert apex.index("intendant_general") < apex.index("controleur_general"), apex


def test_apex_ordre_complet() -> None:
    # From the member upward: intendant general, controleur general, berger des
    # missions, moderateur, fondateur (lowest authority first).
    assert hierarchie_membre._APEX == (
        "intendant_general", "controleur_general", "berger_missions", "moderateur", "fondateur",
    )


def test_builder_chaine_controleur_au_dessus_intendant() -> None:
    # The published chart chain is top-down (founder first); the Controleur General
    # must appear before (above) the Intendant General.
    cles = [fcle for _key, fcle in organigramme_builder._CHAINE]
    assert cles.index("controleur_general") < cles.index("intendant_general"), cles
