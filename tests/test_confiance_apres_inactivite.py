"""Closing an idle session must not cost a trusted device its trust.

The two live in different places on purpose. A session is one sign-in on one browser;
trusting a device is a standing statement that this machine belongs to its owner. So
when a session is closed for inactivity, signing in again asks for the password and
NOT for the one-time code, which is what makes the inactivity delay bearable rather
than punitive. Asked for a code every four hours, nobody would keep the delay on.

These tests pin that the trust check reads the device table alone and never the
session, so a future change to the session lifecycle cannot quietly start demanding a
code on every return.
"""
from __future__ import annotations

import inspect


def test_la_confiance_ne_depend_pas_de_la_session() -> None:
    """The trust lookup reads appareil_confiance, never session."""
    from app.auth import _trusted_device_valid

    source = inspect.getsource(_trusted_device_valid)
    assert "appareil_confiance" in source
    assert "FROM session" not in source, (
        "la confiance accordée à un appareil ne doit pas dépendre d'une session ouverte"
    )


def test_la_fermeture_pour_inactivite_ne_touche_pas_les_appareils() -> None:
    """Closing an idle session touches the session row and nothing else."""
    from app.session_inactivite import fermer_pour_inactivite

    source = inspect.getsource(fermer_pour_inactivite)
    assert "UPDATE session" in source
    assert "appareil_confiance" not in source, (
        "fermer une session inactive ne doit pas révoquer un appareil de confiance"
    )


def test_le_defi_mfa_consulte_bien_la_confiance() -> None:
    """The decision to ask for a code ends on the trusted-device check.

    Without this, a session closed for inactivity would send the person back through
    the full two-factor dance on a machine they already declared theirs.
    """
    from app.auth import _mfa_should_challenge

    source = inspect.getsource(_mfa_should_challenge)
    assert "_trusted_device_valid" in source
    assert "not _trusted_device_valid" in source, (
        "un appareil de confiance doit court-circuiter la demande de code"
    )


def test_les_fenetres_de_confiance_sont_ordonnees() -> None:
    """A shorter window for the accounts that carry more, which is the point of tiers.

    A technical identity is the emergency access, staff and opted-in members come next,
    and an ordinary member gets the longest window. Any other ordering would give the
    most reach the most latitude.
    """
    from app.config import settings

    assert settings.mfa_trust_days_technique < settings.mfa_trust_days_strict
    assert settings.mfa_trust_days_strict < settings.mfa_trust_days


def test_le_reglage_d_inactivite_refuse_une_valeur_absurde() -> None:
    """Bounds are enforced by the reader too, not only by the write endpoint.

    A value written straight into the settings table, by a migration or by hand, must
    not be able to make every session close after one second.
    """
    from app.session_inactivite import MAXIMUM_MINUTES, MINIMUM_MINUTES

    assert MINIMUM_MINUTES >= 5, "un délai plus court se battrait avec l'usage ordinaire"
    assert MAXIMUM_MINUTES <= 20160, "au-delà de deux semaines, le délai ne protège plus rien"
