"""A member must never wait for a code their mailbox has been refusing.

The platform used to say "a code has been sent" while the provider bounced every
message, so the member pressed resend and waited again. These tests cover the
diagnostic that reads the recorded delivery events and turns them into a plain
sentence, and the three guarantees that make it safe to put on the login path: it
cannot raise, it cannot hang, and it cannot leak what the provider said.

The database call is replaced through monkeypatch, which undoes itself. An earlier
version swapped the module in sys.modules and left it there, which broke the tests
that ran next: a test that damages its neighbours is a defect of its own.
"""
from __future__ import annotations

import pytest

from app import email_registre


def _stub(monkeypatch, rows, raises: bool = False, capture: dict | None = None):
    """Make the registry read the given event rows instead of the database."""

    def fetch_all(sql, params, role=None, **kwargs):
        if capture is not None:
            capture.update({"sql": sql, "params": params, "role": role, **kwargs})
        if raises:
            raise RuntimeError("base indisponible")
        return rows

    monkeypatch.setattr(email_registre.db, "fetch_all", fetch_all)


def _bounce(motif: str) -> dict:
    return {"motif": motif}


def test_a_full_mailbox_is_named_in_plain_terms(monkeypatch):
    _stub(monkeypatch, [_bounce("452-4.2.2 The recipient's inbox is out of storage space.")] * 3)
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert etat["probleme"] is True
    assert etat["categorie"] == "boite_pleine"
    assert "pleine" in etat["explication"]
    assert etat["occurrences"] == 3


@pytest.mark.parametrize("motif,categorie,attendu", [
    ("552 5.2.2 Over quota", "quota_atteint", "quota"),
    ("550 5.1.1 User unknown", "adresse_inconnue", "n'existe pas"),
    ("452 4.3.1 Insufficient system storage", "serveur_sature", "espace"),
    ("550 mailbox full", "boite_pleine", "pleine"),
])
def test_each_known_mailbox_refusal_gets_its_own_wording(monkeypatch, motif, categorie, attendu):
    _stub(monkeypatch, [_bounce(motif)])
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert etat["categorie"] == categorie
    assert attendu in etat["explication"]


def test_an_unknown_refusal_still_warns_without_guessing(monkeypatch):
    _stub(monkeypatch, [_bounce("571 policy rejection")])
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert etat["probleme"] is True
    assert etat["categorie"] == "refus_non_qualifie"
    assert "refusé" in etat["explication"]      # honest, no invented cause


def test_the_provider_raw_reason_never_leaves_the_module(monkeypatch):
    """Bounce text quotes the recipient address and sometimes the subject.

    What is never returned can never leak into an API response later.
    """
    _stub(monkeypatch, [_bounce("<membre@example.org>: mailbox full, subject 'Votre code'")])
    etat = email_registre.diagnostic_boite("membre@example.org")
    assert "membre@example.org" not in str(etat)
    assert "subject" not in str(etat).lower()


def test_no_recent_bounce_means_no_warning(monkeypatch):
    _stub(monkeypatch, [])
    assert email_registre.diagnostic_boite("membre@example.org")["probleme"] is False


def test_an_empty_address_is_not_queried(monkeypatch):
    """No address, no query: the diagnostic must not reach the database at all."""
    def explode(*args, **kwargs):
        raise AssertionError("the database was queried for an empty address")

    monkeypatch.setattr(email_registre.db, "fetch_all", explode)
    assert email_registre.diagnostic_boite("")["probleme"] is False


def test_a_database_failure_never_blocks_the_login(monkeypatch, caplog):
    """The diagnostic is observability: it must fail silent to the member, never raise.

    Silent to the member, not silent in the logs: a diagnostic that dies unnoticed
    recreates the very blindness this module exists to end.
    """
    _stub(monkeypatch, [], raises=True)
    with caplog.at_level("WARNING"):
        assert email_registre.diagnostic_boite("membre@example.org")["probleme"] is False
    assert "diagnostic de boîte indisponible" in caplog.text
    assert "membre@example.org" not in caplog.text     # never the address


def test_the_query_is_bounded_so_it_can_never_hold_up_a_login(monkeypatch):
    """It cannot raise is not enough on the login path; it must not be able to hang."""
    vu: dict = {}
    _stub(monkeypatch, [], capture=vu)
    email_registre.diagnostic_boite("membre@example.org", role="membre")

    assert vu["statement_timeout_ms"] == email_registre._DELAI_DIAGNOSTIC_MS
    assert vu["connect_timeout_s"] == email_registre._DELAI_CONNEXION_S
    assert vu["role"] == "membre"                       # read under RLS, not as owner
    assert "LIMIT %s" in vu["sql"]
    assert vu["params"][-1] == email_registre._PLAFOND_EVENEMENTS


def test_the_query_matches_the_index_created_for_it(monkeypatch):
    """Migration 0187 indexes lower(destinataire) restricted to refusals.

    A predicate that drifts from that shape silently becomes a sequential scan on a
    table that only grows, on the one path where slowness is least acceptable.
    """
    vu: dict = {}
    _stub(monkeypatch, [], capture=vu)
    email_registre.diagnostic_boite("membre@example.org")

    sql = " ".join(vu["sql"].split())
    assert "lower(destinataire) = lower(%s)" in sql
    assert "statut_normalise IN ('rebondi', 'rejete')" in sql
    assert "ORDER BY survenu_le DESC" in sql


def test_a_bad_window_is_a_programming_error_not_a_silent_outage(monkeypatch):
    """int(heures) sits outside the guard on purpose: a bug must be visible."""
    _stub(monkeypatch, [])
    with pytest.raises((ValueError, TypeError)):
        email_registre.diagnostic_boite("membre@example.org", heures="deux jours")
