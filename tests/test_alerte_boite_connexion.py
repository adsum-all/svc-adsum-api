"""What the sign-in screen tells someone whose mailbox is refusing the code.

The diagnostic itself is covered next door, in test_diagnostic_boite_email. These
tests cover the sentence built from it, because that sentence is the only thing the
person locked out ever sees, and a wrong one costs them the session.

The case that matters most is the technical super-administrator: it carries no member
row, so it has no Telegram link and cannot obtain one from the sign-in screen. The
message used to end with "you can also receive the code by Telegram", which sent the
one account already locked out down a route that does not exist for it.
"""
from __future__ import annotations

import pytest

from app import auth

_PLEINE = {
    "probleme": True,
    "categorie": "boite_pleine",
    "explication": "la boîte de réception est pleine",
    "occurrences": 9,
}
_SAINE = {"probleme": False, "categorie": "", "explication": "", "occurrences": 0}


@pytest.fixture
def _diagnostic(monkeypatch):
    """Drive the diagnostic verdict without touching a database."""

    def poser(verdict: dict) -> None:
        monkeypatch.setattr(
            auth.email_registre, "diagnostic_boite", lambda *a, **k: verdict,
        )

    return poser


@pytest.fixture
def _telegram(monkeypatch):
    """Say whether the account has a Telegram link, without touching a database."""

    def poser(lie: bool) -> None:
        monkeypatch.setattr(auth, "_telegram_chat_id", lambda _uid: "42" if lie else None)

    return poser


def test_a_full_mailbox_is_reported_with_what_to_do(_diagnostic, _telegram):
    _diagnostic(_PLEINE)
    _telegram(False)
    message = auth._alerte_boite("membre@example.org", "email", "membre", "u-1")
    assert message is not None
    assert "la boîte de réception est pleine" in message
    assert "Libérez de l'espace" in message


def test_telegram_is_offered_only_to_an_account_that_has_it(_diagnostic, _telegram):
    _diagnostic(_PLEINE)
    _telegram(True)
    assert "Telegram" in auth._alerte_boite("membre@example.org", "email", "membre", "u-1")


def test_an_account_without_telegram_is_never_sent_to_telegram(_diagnostic, _telegram):
    """The exact defect: advising a channel the locked-out account cannot use."""
    _diagnostic(_PLEINE)
    _telegram(False)
    message = auth._alerte_boite("admin@example.org", "email", "super_admin", "u-2")
    assert "Telegram" not in message
    assert "la vider est la seule façon" in message


def test_no_user_id_means_no_telegram_suggestion(_diagnostic, monkeypatch):
    """A missing id must not be read as "has Telegram", nor be looked up at all.

    Patched through monkeypatch, which undoes itself: a test that leaves the module
    replaced breaks whichever test runs next, and that failure looks like a defect in
    the neighbour rather than here.
    """

    def explode(_uid):
        raise AssertionError("the Telegram link was looked up without an account id")

    _diagnostic(_PLEINE)
    monkeypatch.setattr(auth, "_telegram_chat_id", explode)
    message = auth._alerte_boite("membre@example.org", "email", "membre", None)
    assert "Telegram" not in message


def test_a_healthy_mailbox_says_nothing(_diagnostic, _telegram):
    _diagnostic(_SAINE)
    _telegram(True)
    assert auth._alerte_boite("membre@example.org", "email", "membre", "u-1") is None


@pytest.mark.parametrize("canal", ["telegram", "sms", "", None])
def test_a_code_sent_elsewhere_never_mentions_the_mailbox(_diagnostic, _telegram, canal):
    """The mailbox is not on the path, so its state is not the member's problem."""
    _diagnostic(_PLEINE)
    _telegram(False)
    assert auth._alerte_boite("membre@example.org", canal, "membre", "u-1") is None


def test_the_provider_raw_reason_is_never_shown_to_the_member(_diagnostic, _telegram):
    """Bounce text quotes the address and sometimes the subject of the message."""
    _diagnostic({
        "probleme": True,
        "categorie": "boite_pleine",
        "explication": "la boîte de réception est pleine",
        "occurrences": 3,
    })
    _telegram(False)
    message = auth._alerte_boite("membre@example.org", "email", "membre", "u-1")
    assert "membre@example.org" not in message
    assert "452" not in message
