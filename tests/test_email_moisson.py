"""Fetching what became of the messages, and above all failing to, without damage.

This runs first inside the daily job. Everything here is about it staying harmless:
it must never raise into that job, never double-count an event it already recorded,
and never claim to have collected everything when it stopped at its cap.

The last point is the one that matters most. This module exists because the platform
was blind to its own deliveries and told a locked-out administrator that a code had
been sent. A collector that quietly truncates recreates exactly that blindness in a
smaller form.

No network and no database: both are replaced.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app import email_moisson


@pytest.fixture
def _cle(monkeypatch):
    monkeypatch.setattr(email_moisson, "_cle_api", lambda: "cle-de-test")


@pytest.fixture
def _pages(monkeypatch):
    """Serve the given pages of events, then nothing."""

    def poser(pages: list[list[dict]]) -> list[int]:
        appels: list[int] = []

        def page(_cle, rang, _ctx):
            appels.append(rang)
            return pages[rang] if rang < len(pages) else []

        monkeypatch.setattr(email_moisson, "_page", page)
        return appels

    return poser


@pytest.fixture
def _ecrits(monkeypatch):
    """Capture what would be written, and answer the outbox lookup with nothing."""
    ecrits: list[tuple] = []
    monkeypatch.setattr(email_moisson.db, "fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(email_moisson.db, "execute", lambda sql, params=(), **k: ecrits.append((sql, params)))
    return ecrits


def _evenement(adresse: str = "membre@example.org", genre: str = "delivered") -> dict:
    return {"email": adresse, "event": genre, "date": "2026-08-02T10:03:00Z", "reason": None}


def test_without_a_sending_key_the_collector_says_it_is_off(monkeypatch):
    """Silence is not success: the daily job must be able to tell them apart."""
    monkeypatch.setattr(email_moisson, "_cle_api", lambda: "")
    resultat = email_moisson.moissonner()
    assert resultat["actif"] is False
    assert "clé" in resultat["motif"]


def test_events_are_fetched_and_written(_cle, _pages, _ecrits):
    _pages([[_evenement(), _evenement("autre@example.org", "softBounces")]])
    resultat = email_moisson.moissonner()
    assert resultat["actif"] is True
    assert resultat["recuperes"] == 2
    assert resultat["examines"] == 2
    assert _ecrits, "rien n'a été écrit"


def test_paging_stops_at_the_first_empty_page(_cle, _pages, _ecrits):
    """Twenty requests for three events would cost the daily job twenty round trips."""
    appels = _pages([[_evenement()], []])
    email_moisson.moissonner()
    assert appels == [0, 1]


def test_an_event_already_recorded_is_not_counted_twice(_cle, _pages, _ecrits):
    """The provider reports the same event on every run inside the window.

    Counting it again would turn one bounce into a pattern, and a pattern is what the
    login screen reads to tell a member their mailbox is refusing mail.
    """
    _pages([[_evenement()]])
    email_moisson.moissonner()
    sql = " ".join(_ecrits[0][0].split())
    assert "WHERE NOT EXISTS" in sql
    assert "lower(x.destinataire) = lower(v.b)" in sql
    assert "x.evenement_fournisseur = v.c" in sql
    assert "x.survenu_le = v.g" in sql


def test_the_provider_timestamp_is_kept_not_the_moment_we_heard_of_it(_cle, _pages, _ecrits):
    """A run that imports three days of history must not date it all at now()."""
    _pages([[_evenement()]])
    email_moisson.moissonner()
    _, params = _ecrits[0]
    assert "2026-08-02T10:03:00Z" in params


def test_an_event_without_a_recipient_is_skipped(_cle, _pages, _ecrits):
    _pages([[{"event": "delivered", "date": "2026-08-02T10:03:00Z"}, _evenement()]])
    resultat = email_moisson.moissonner()
    assert resultat["recuperes"] == 2
    assert resultat["examines"] == 1


def test_reaching_the_cap_is_reported_rather_than_passed_over(_cle, _pages, _ecrits):
    """A silent cap reads as "everything was collected", which is the very blindness
    this module exists to end."""
    plein = [[_evenement() for _ in range(email_moisson._PAR_PAGE)] for _ in range(email_moisson._PAGES_MAX)]
    _pages(plein)
    resultat = email_moisson.moissonner()
    assert resultat["tronque"] is True
    assert resultat["recuperes"] == email_moisson._PAR_PAGE * email_moisson._PAGES_MAX


def test_a_normal_run_is_not_reported_as_truncated(_cle, _pages, _ecrits):
    _pages([[_evenement()]])
    assert email_moisson.moissonner()["tronque"] is False


def test_a_provider_error_is_reported_never_raised(_cle, monkeypatch, _ecrits):
    """This runs first in the daily job: raising costs every task that follows."""

    def explose(*_a):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr(email_moisson, "_page", explose)
    resultat = email_moisson.moissonner()
    assert resultat["actif"] is True
    assert resultat["erreur"] == "HTTP 401"


def test_a_network_failure_is_reported_never_raised(_cle, monkeypatch, _ecrits):
    def explose(*_a):
        raise TimeoutError("delai depasse")

    monkeypatch.setattr(email_moisson, "_page", explose)
    assert email_moisson.moissonner()["erreur"] == "TimeoutError"


def test_a_database_failure_is_reported_never_raised(_cle, _pages, monkeypatch):
    _pages([[_evenement()]])
    monkeypatch.setattr(email_moisson.db, "fetch_all", lambda *a, **k: [])

    def explose(*_a, **_k):
        raise RuntimeError("base indisponible")

    monkeypatch.setattr(email_moisson.db, "execute", explose)
    resultat = email_moisson.moissonner()
    assert resultat["recuperes"] == 1
    assert "écriture" in resultat["erreur"]


@pytest.mark.parametrize("genre,attendu", [
    ("delivered", "delivre"),
    ("softBounces", "rebondi"),
    ("hardBounces", "rebondi"),
    ("blocked", "rejete"),
    ("spam", "rejete"),
    ("opened", "ouvert"),
])
def test_the_provider_vocabulary_maps_to_the_ledger(_cle, _pages, _ecrits, genre, attendu):
    """A bounce filed as anything but 'rebondi' is a bounce the diagnostic never reads."""
    _pages([[_evenement(genre=genre)]])
    email_moisson.moissonner()
    assert attendu in _ecrits[0][1]


def test_an_unknown_event_is_kept_rather_than_guessed_at(_cle, _pages, _ecrits):
    _pages([[_evenement(genre="quelque_chose_de_nouveau")]])
    email_moisson.moissonner()
    _, params = _ecrits[0]
    assert "quelque_chose_de_nouveau" in params   # kept verbatim
    assert "en_cours" in params                   # and filed as undecided, not as delivered


def test_the_whole_event_is_kept_so_a_later_question_can_be_answered(_cle, _pages, _ecrits):
    """The reason text is what told us a mailbox was full. Dropping the rest of the
    payload would mean the next unexplained failure has nothing to read."""
    brut = {"email": "m@example.org", "event": "softBounces", "date": "2026-08-02T10:03:00Z",
            "reason": "452-4.2.2 out of storage space", "messageId": "abc", "tag": "login"}
    _pages([[brut]])
    email_moisson.moissonner()
    _, params = _ecrits[0]
    charge = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(charge)["messageId"] == "abc"
