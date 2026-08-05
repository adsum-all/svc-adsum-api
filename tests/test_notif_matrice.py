"""Unit tests for the per-group, per-channel notification matrix.

Proves a member can mute a notification group on one channel (e.g. e-mail) while
keeping it on another (Telegram), that critical messages ignore the matrix, that
in-app is always delivered, and that the stored matrix is sanitised.
"""
from __future__ import annotations

from unittest import mock

from app import channels
from app import notifications as N
from app.formation import _merge_matrice, _sanitize_matrice


def _prefs(matrice=None, **over):
    # push defaults to on, like email and telegram and unlike whatsapp and sms:
    # somebody who installed the mobile application and registered a device asked to
    # be reached on it, whereas the paid channels stay off until chosen.
    base = {
        "matrice_canaux": matrice, "email": True, "telegram": True, "whatsapp": False,
        "sms": False, "push": True,
        "evenements": True, "demandes": True, "rappels": True, "anniversaire": True,
        "anniv_pairs": True, "collaboration": True,
    }
    base.update(over)
    return base


def _patch_prefs(prefs):
    def fake_fetch_one(sql, params, role=None):
        return dict(prefs) if "preference_notification" in sql else None
    return mock.patch.object(N.db, "fetch_one", fake_fetch_one)


# ---- canaux_autorises ----

def test_group_email_off_keeps_telegram():
    m = {"rappels": {"email": False, "telegram": True}}
    with _patch_prefs(_prefs(m)):
        # push is absent from this group's entry, so it keeps its default: muting one
        # channel must never mute a channel the member did not name.
        assert N.canaux_autorises("M", None, "activite_rappel_j1", "operationnel") == {"telegram", "push"}


def test_group_all_on_default():
    with _patch_prefs(_prefs(None)):
        # No matrix, legacy category on -> every free channel
        assert N.canaux_autorises("M", None, "demande_reponse", "prive") == {"email", "telegram", "push"}


def test_a_group_muted_on_push_keeps_the_other_channels():
    """The mobile application is one channel among several, silenced on its own."""
    m = {"rappels": {"push": False}}
    with _patch_prefs(_prefs(m)):
        assert N.canaux_autorises("M", None, "activite_rappel_j1", "operationnel") == {"email", "telegram"}


def test_master_push_off_blocks_every_group():
    """Turning the channel off wholesale must not need a pass over every group."""
    m = {"rappels": {"email": True, "telegram": True, "push": True}}
    with _patch_prefs(_prefs(m, push=False)):
        assert N.canaux_autorises("M", None, "activite_rappel_j1", "operationnel") == {"email", "telegram"}


def test_critical_is_never_gated():
    with _patch_prefs(_prefs({"rappels": {"email": False, "telegram": False}})):
        assert N.canaux_autorises("M", None, "otp", "critique") is None


def test_ungrouped_type_not_gated():
    # A genuinely ungrouped, action-needed type stays ungated (master switches only).
    with _patch_prefs(_prefs(None)):
        assert N.canaux_autorises("M", None, "modification_complement", "informationnel") is None


def test_announcements_and_attendance_are_now_gated():
    # Announcements and the attendance survey (pointage) are member-controllable per channel
    # now (on/off), instead of bypassing the matrix. Default: both channels on.
    with _patch_prefs(_prefs(None)):
        assert N.canaux_autorises("M", None, "annonce", "informationnel") == {"email", "telegram", "push"}
        assert N.canaux_autorises("M", None, "sondage_activite", "operationnel") == {"email", "telegram", "push"}
    # A member can now silence the e-mail channel for pointage while keeping Telegram.
    with _patch_prefs(_prefs({"pointage": {"email": False, "telegram": True, "push": False}})):
        assert N.canaux_autorises("M", None, "sondage_activite", "operationnel") == {"telegram"}


def test_master_email_off_blocks_even_if_group_on():
    m = {"rappels": {"email": True, "telegram": True}}
    with _patch_prefs(_prefs(m, email=False)):
        assert N.canaux_autorises("M", None, "activite_rappel_j1", "operationnel") == {"telegram", "push"}


def test_legacy_category_off_without_matrix_mutes_both():
    with _patch_prefs(_prefs(None, rappels=False)):
        assert N.canaux_autorises("M", None, "activite_rappel_j1", "operationnel") == set()


# ---- dispatch honours canaux_autorises, in-app always on ----

def test_dispatch_filters_push_but_keeps_in_app():
    prefs = {"email": True, "telegram": True, "whatsapp": False, "sms": False}
    contact = {
        "email": "a@b.c", "telegram_chat_id": "C", "whatsapp_numero": None,
        "indicatif_telephone": None, "telephone": None,
    }
    sent = []

    def fake_fetch_one(sql, params, role=None):
        if "preference_notification" in sql:
            return dict(prefs)
        if "FROM membre" in sql:
            return dict(contact)
        return None

    with mock.patch.object(channels.db, "fetch_one", fake_fetch_one), \
         mock.patch.object(channels.db, "execute", lambda *a, **k: None), \
         mock.patch.object(channels, "send_email", lambda *a, **k: (sent.append("email"), (True, "x"))[1]), \
         mock.patch.object(channels, "send_telegram", lambda *a, **k: (sent.append("telegram"), True)[1]), \
         mock.patch.object(channels, "canal_actif", lambda c: True):
        used = channels.dispatch("M", None, channels.Message(titre="t", corps_text="c"), canaux_autorises={"telegram"})
    assert "in-app" in used
    assert sent == ["telegram"]  # e-mail filtered out by the matrix, in-app still recorded


# ---- sanitisation ----

def test_sanitize_drops_unknown_groups_and_channels():
    raw = {
        "rappels": {"email": False, "telegram": True, "hack": True},
        "inconnu": {"email": True},
        "dossier": {"sms": 1},
    }
    clean = _sanitize_matrice(raw, N.GROUPES)
    assert clean["rappels"] == {"email": False, "telegram": True}
    assert "inconnu" not in clean
    assert clean["dossier"] == {"sms": True}


def test_sanitize_non_dict_returns_none():
    assert _sanitize_matrice("nope", N.GROUPES) is None
    assert _sanitize_matrice({}, N.GROUPES) is None


# ---- merge never wipes a stored choice (MAJEUR fix) ----

def test_merge_none_incoming_keeps_stored():
    stored = {"rappels": {"email": False, "telegram": True}}
    assert _merge_matrice(stored, None) == {"rappels": {"email": False, "telegram": True}}


def test_merge_partial_incoming_preserves_other_groups():
    stored = {"rappels": {"email": False, "telegram": True}, "recap": {"email": True, "telegram": True}}
    incoming = {"recap": {"email": False}}
    merged = _merge_matrice(stored, incoming)
    assert merged["rappels"] == {"email": False, "telegram": True}  # untouched
    assert merged["recap"] == {"email": False, "telegram": True}  # only email overridden


def test_merge_both_empty_is_none():
    assert _merge_matrice(None, None) is None
