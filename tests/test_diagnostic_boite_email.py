"""A member must never wait for a code their mailbox has been refusing.

The platform used to say "a code has been sent" while the provider bounced every
message, so the member pressed resend and waited again. These tests cover the
diagnostic that reads the recorded delivery events and turns them into a plain
sentence, and the guarantee that it can never block a login.

The database call is replaced through monkeypatch, which undoes itself. An earlier
version swapped the module in sys.modules and left it there, which broke the tests
that ran next: a test that damages its neighbours is a defect of its own.
"""
from __future__ import annotations

import pytest

from app import email_registre


def _stub(monkeypatch, rows, raises: bool = False):
    """Make the registry read the given event rows instead of the database."""

    def fetch_all(sql, params, role=None):
        if raises:
            raise RuntimeError("base indisponible")
        return rows

    monkeypatch.setattr(email_registre.db, "fetch_all", fetch_all)


def _bounce(motif: str) -> dict:
    return {"motif": motif, "survenu_le": "2026-07-31T22:14:09"}


def test_a_full_mailbox_is_named_in_plain_terms(monkeypatch):
    _stub(monkeypatch, [_bounce("452-4.2.2 The recipient's inbox is out of storage space.")] * 3)
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert etat["probleme"] is True
    assert "pleine" in etat["explication"]
    assert etat["occurrences"] == 3
    assert etat["motif"].startswith("452-4.2.2")


@pytest.mark.parametrize("motif,attendu", [
    ("552 5.2.2 Over quota", "quota"),
    ("550 5.1.1 User unknown", "n'existe pas"),
    ("452 4.3.1 Insufficient system storage", "espace"),
])
def test_each_known_mailbox_refusal_gets_its_own_wording(monkeypatch, motif, attendu):
    _stub(monkeypatch, [_bounce(motif)])
    assert attendu in email_registre.diagnostic_boite("membre@example.org")["explication"]


def test_an_unknown_refusal_still_warns_without_guessing(monkeypatch):
    _stub(monkeypatch, [_bounce("571 policy rejection")])
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert etat["probleme"] is True
    assert "refusé" in etat["explication"]      # honest, no invented cause


def test_no_recent_bounce_means_no_warning(monkeypatch):
    _stub(monkeypatch, [])
    assert email_registre.diagnostic_boite("membre@example.org")["probleme"] is False


def test_an_empty_address_is_not_queried(monkeypatch):
    """No address, no query: the diagnostic must not reach the database at all."""
    def explode(*args, **kwargs):
        raise AssertionError("the database was queried for an empty address")

    monkeypatch.setattr(email_registre.db, "fetch_all", explode)
    assert email_registre.diagnostic_boite("")["probleme"] is False


def test_a_database_failure_never_blocks_the_login(monkeypatch):
    """The diagnostic is observability: it must fail silent, never raise."""
    _stub(monkeypatch, [], raises=True)
    assert email_registre.diagnostic_boite("membre@example.org")["probleme"] is False
