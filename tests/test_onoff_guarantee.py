"""Guarantee: a channel turned OFF is really OFF, a channel turned ON is really ON.

Exercises the REAL channels.dispatch, intercepting only the actual network sends so we can
observe exactly which channels are attempted. Proves both directions for both channels, so
a member's personal On/Off choice is always respected and nothing was broken by routing the
member confirmations through dispatch.
"""
# ruff: noqa: E501 - test data lines are long by nature
from __future__ import annotations

from unittest import mock

from app import channels


def _run(prefs: dict, canaux_autorises=None, critique: bool = False) -> list[str]:
    contact = {"email": "a@b.c", "telegram_chat_id": "C", "whatsapp_numero": None, "indicatif_telephone": None, "telephone": None}
    sent: list[str] = []

    def fake_fetch_one(sql, params, role=None):
        if "preference_notification" in sql:
            return dict(prefs)
        if "FROM membre" in sql:
            return dict(contact)
        return None

    with mock.patch.object(channels.db, "fetch_one", fake_fetch_one), \
         mock.patch.object(channels.db, "execute", lambda *a, **k: None), \
         mock.patch.object(channels, "send_email", lambda *a, **k: (sent.append("email"), (True, "ok"))[1]), \
         mock.patch.object(channels, "send_telegram", lambda *a, **k: (sent.append("telegram"), True)[1]), \
         mock.patch.object(channels, "canal_actif", lambda c: True):
        channels.dispatch("M", None, channels.Message(titre="t", corps_text="c"), critique=critique, canaux_autorises=canaux_autorises)
    return sent


_ON = {"email": True, "telegram": True, "whatsapp": False, "sms": False}


def test_both_on_both_sent() -> None:
    # ON = really ON: e-mail AND Telegram are actually sent.
    sent = _run(_ON)
    assert "email" in sent and "telegram" in sent


def test_email_off_email_not_sent_telegram_still_sent() -> None:
    # E-mail OFF = really OFF; Telegram stays ON.
    sent = _run({**_ON, "email": False})
    assert "email" not in sent
    assert "telegram" in sent


def test_telegram_off_telegram_not_sent_email_still_sent() -> None:
    # Telegram OFF = really OFF; e-mail stays ON.
    sent = _run({**_ON, "telegram": False})
    assert "telegram" not in sent
    assert "email" in sent


def test_group_email_off_via_matrix_blocks_email_only() -> None:
    # Per-notification (matrix) choice: e-mail off for this group -> only Telegram sent.
    sent = _run(_ON, canaux_autorises={"telegram"})
    assert sent == ["telegram"]


def test_group_both_on_via_matrix_sends_both() -> None:
    sent = _run(_ON, canaux_autorises={"email", "telegram"})
    assert "email" in sent and "telegram" in sent


def test_security_message_always_sent_regardless_of_off() -> None:
    # A critical security message (login code) reaches the member even if both are off.
    sent = _run({"email": False, "telegram": False, "whatsapp": False, "sms": False}, critique=True)
    assert "email" in sent and "telegram" in sent
