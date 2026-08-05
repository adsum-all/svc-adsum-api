"""Reaching the mobile application, and above all failing to, without collateral.

Push is one channel among five. Everything here is about it staying that way: it must
never raise into the notification funnel, never retire a phone over a passing error,
and never pretend to have delivered when the organisation has configured nothing.

The device token is what makes this delicate. It rotates on its own, so the same
phone reappears under a new one; it is reissued to a reinstalled application, so it
can legitimately change hands; and the service reports a dead one in the same shape
as a temporary outage. Confusing those loses either a member's notifications or a
member's phone.

No network and no database: both are replaced.
"""
from __future__ import annotations

import urllib.error

import pytest

from app import push

_MEMBRE = "11111111-1111-1111-1111-111111111111"
#: A service account shaped like the real one and holding nothing. The signing key is
#: a marker, never a PEM block: every test that would use it replaces jwt.encode, so
#: a real key would add no coverage and would sit in the repository looking like one.
_COMPTE = {
    "client_email": "envoi@exemple.iam.gserviceaccount.com",
    "private_key": "cle-de-test-non-signante",
    "project_id": "exemple-1234",
}


@pytest.fixture(autouse=True)
def _sans_cache():
    """Each test starts without a cached access token."""
    push._acces["jeton"] = ""
    push._acces["expire"] = 0.0


@pytest.fixture
def _configure(monkeypatch):
    """Pretend the organisation set a service account, and skip the OAuth exchange."""
    monkeypatch.setattr(push, "_compte_de_service", lambda: _COMPTE)
    monkeypatch.setattr(push, "_jeton_acces", lambda _c: "jeton-acces")


@pytest.fixture
def _appareils(monkeypatch):
    def poser(liste):
        monkeypatch.setattr(push, "appareils", lambda *a, **k: liste)

    return poser


@pytest.fixture
def _ecritures(monkeypatch):
    """Record what would be written, instead of writing it."""
    vues: list[tuple] = []
    monkeypatch.setattr(push.db, "execute", lambda sql, params=(), **k: vues.append((sql, params)))
    return vues


def test_without_a_service_account_the_channel_reports_itself_off(monkeypatch):
    """Silence is not delivery: dispatch must be able to tell them apart."""
    monkeypatch.setattr(push.channels, "integration_value", lambda _c: "")
    assert push.configure() is False
    assert push.envoyer(_MEMBRE, "titre", "corps") is False


def test_a_malformed_service_account_is_refused_rather_than_half_used(monkeypatch):
    monkeypatch.setattr(push.channels, "integration_value", lambda _c: "{ pas du json")
    assert push.configure() is False


def test_a_service_account_missing_a_field_is_refused(monkeypatch):
    import json

    monkeypatch.setattr(
        push.channels, "integration_value",
        lambda _c: json.dumps({"client_email": "x@y.z", "project_id": "p"}),  # no private_key
    )
    assert push.configure() is False


def test_a_member_with_no_registered_device_is_not_a_failure(_configure, _appareils):
    _appareils([])
    assert push.envoyer(_MEMBRE, "titre", "corps") is False


def test_a_delivered_message_marks_the_channel_as_used(_configure, _appareils, _ecritures, monkeypatch):
    _appareils([{"id": "a1", "jeton": "jeton-1"}])
    monkeypatch.setattr(push, "_envoyer_a", lambda *a: (True, ""))
    assert push.envoyer(_MEMBRE, "titre", "corps") is True
    assert any("envoye_le" in sql for sql, _ in _ecritures)


@pytest.mark.parametrize("motif", ["UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"])
def test_a_token_the_service_calls_dead_retires_the_device(_configure, _appareils, _ecritures, monkeypatch, motif):
    """Otherwise a wiped phone is retried on every notification, forever."""
    _appareils([{"id": "a1", "jeton": "jeton-mort"}])
    monkeypatch.setattr(push, "_envoyer_a", lambda *a: (False, motif))
    assert push.envoyer(_MEMBRE, "titre", "corps") is False
    retraits = [p for sql, p in _ecritures if "actif = false" in sql]
    assert retraits, "l'appareil refusé n'a pas été retiré"
    assert motif in retraits[0][0]


@pytest.mark.parametrize("motif", ["UNAVAILABLE", "INTERNAL", "HTTP 503", "TimeoutError"])
def test_a_passing_failure_never_costs_the_member_their_phone(_configure, _appareils, _ecritures, monkeypatch, motif):
    """A provider outage must not unregister every device on the platform."""
    _appareils([{"id": "a1", "jeton": "jeton-1"}])
    monkeypatch.setattr(push, "_envoyer_a", lambda *a: (False, motif))
    assert push.envoyer(_MEMBRE, "titre", "corps") is False
    assert not [p for sql, p in _ecritures if "actif = false" in sql]


def test_one_dead_device_does_not_stop_the_others(_configure, _appareils, _ecritures, monkeypatch):
    _appareils([{"id": "a1", "jeton": "mort"}, {"id": "a2", "jeton": "vivant"}])
    monkeypatch.setattr(push, "_envoyer_a",
                        lambda _a, _p, jeton, *r: (jeton == "vivant", "" if jeton == "vivant" else "UNREGISTERED"))
    assert push.envoyer(_MEMBRE, "titre", "corps") is True


def test_a_long_body_is_trimmed_rather_than_lost(_configure, _appareils, monkeypatch):
    """Past 4 KB the service refuses the whole message, not just its tail."""
    vus: list[str] = []
    _appareils([{"id": "a1", "jeton": "jeton-1"}])
    monkeypatch.setattr(push, "_envoyer_a",
                        lambda _a, _p, _j, _t, corps, _d: (vus.append(corps), (True, ""))[1])
    push.envoyer(_MEMBRE, "titre", "x" * 5000)
    assert len(vus[0]) <= 500
    assert vus[0].endswith("...")


def test_a_short_body_is_sent_whole(_configure, _appareils, monkeypatch):
    vus: list[str] = []
    _appareils([{"id": "a1", "jeton": "jeton-1"}])
    monkeypatch.setattr(push, "_envoyer_a",
                        lambda _a, _p, _j, _t, corps, _d: (vus.append(corps), (True, ""))[1])
    push.envoyer(_MEMBRE, "titre", "Réunion demain à 18 h.")
    assert vus[0] == "Réunion demain à 18 h."


def test_a_database_outage_while_sending_never_raises(_configure, _appareils, monkeypatch):
    """This runs inside the notification funnel: raising costs every other channel."""
    def explose(*a, **k):
        raise RuntimeError("base indisponible")

    _appareils([{"id": "a1", "jeton": "jeton-1"}])
    monkeypatch.setattr(push, "_envoyer_a", lambda *a: (True, ""))
    monkeypatch.setattr(push.db, "execute", explose)
    assert push.envoyer(_MEMBRE, "titre", "corps") is True   # delivered, stamp lost


def test_registering_a_device_reassigns_a_token_that_changed_hands(_ecritures):
    """The service reissues a token to a reinstalled application. Refusing it would
    leave notifications following a phone to whoever signed in on it first."""
    assert push.enregistrer_appareil(_MEMBRE, "jeton-1", "android", "Pixel") is True
    sql, params = _ecritures[0]
    assert "ON CONFLICT (jeton) DO UPDATE" in sql
    assert "membre_id = EXCLUDED.membre_id" in sql
    assert "actif = true" in sql            # a device that came back is live again
    assert "motif_retrait = NULL" in sql    # and no longer carries an old refusal


def test_an_empty_token_is_refused_before_reaching_the_database(monkeypatch):
    def explose(*a, **k):
        raise AssertionError("la base a été appelée pour un jeton vide")

    monkeypatch.setattr(push.db, "execute", explose)
    assert push.enregistrer_appareil(_MEMBRE, "   ") is False
    assert push.enregistrer_appareil("", "jeton-1") is False


def test_an_unknown_platform_falls_back_rather_than_violating_the_check(_ecritures):
    """The column carries a CHECK; an unknown value would raise on insert."""
    push.enregistrer_appareil(_MEMBRE, "jeton-1", "symbian")
    assert _ecritures[0][1][2] == "android"


def test_the_access_token_is_reused_until_it_nears_expiry(monkeypatch):
    """One OAuth exchange per hour, not one per notification."""
    appels: list[int] = []

    class Reponse:
        def __enter__(self):
            appels.append(1)
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token": "abc", "expires_in": 3600}'

    monkeypatch.setattr(push.jwt, "encode", lambda *a, **k: "assertion")
    monkeypatch.setattr(push.urllib.request, "urlopen", lambda *a, **k: Reponse())
    monkeypatch.setattr(push.json, "load", lambda _f: {"access_token": "abc", "expires_in": 3600})

    assert push._jeton_acces(_COMPTE) == "abc"
    assert push._jeton_acces(_COMPTE) == "abc"
    assert len(appels) == 1


def test_a_refused_oauth_exchange_is_reported_not_raised(monkeypatch):
    def explose(*a, **k):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr(push.jwt, "encode", lambda *a, **k: "assertion")
    monkeypatch.setattr(push.urllib.request, "urlopen", explose)
    assert push._jeton_acces(_COMPTE) is None
