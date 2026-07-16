"""Real integration proof of the On/Off guarantee, against the REAL database.

Reads and writes REAL preference rows for a demo member and runs the REAL channels.dispatch,
so the decision comes from real data, not fabricated dicts. Only the external network sends
(Brevo e-mail, Telegram API) are intercepted, which is the sole mocking the project permits
(unreachable external calls). Skips automatically when no database is configured.
"""
# ruff: noqa: E501 - SQL lines are long by nature
from __future__ import annotations

import os
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ADSUM_DATABASE_URL"), reason="requires the real database")

from app import channels, db  # noqa: E402

ROLE = "super_admin"


def _demo_membre() -> str | None:
    row = db.fetch_one("SELECT id FROM membre WHERE email LIKE %s AND statut = 'actif' ORDER BY cree_le LIMIT 1", ("%@exemple.com",), role=ROLE)
    return str(row["id"]) if row else None


def _set_pref(mid: str, email: bool, telegram: bool) -> None:
    db.execute(
        "INSERT INTO preference_notification (membre_id, email, telegram) VALUES (%s, %s, %s) "
        "ON CONFLICT (membre_id) DO UPDATE SET email = EXCLUDED.email, telegram = EXCLUDED.telegram",
        (mid, email, telegram), role=ROLE,
    )


def _attempts(mid: str) -> list[str]:
    """Run the REAL dispatch; record only which external sends were attempted."""
    sent: list[str] = []
    with mock.patch.object(channels, "send_email", lambda *a, **k: (sent.append("email"), (True, "ok"))[1]), \
         mock.patch.object(channels, "send_telegram", lambda *a, **k: (sent.append("telegram"), True)[1]):
        channels.dispatch(mid, ROLE, channels.Message(titre="t", corps_text="c", type_notif="test_onoff"), canaux_autorises=None)
    return sent


def test_onoff_guarantee_against_real_db() -> None:
    mid = _demo_membre()
    if not mid:
        pytest.skip("no demo member")
    orig = db.fetch_one("SELECT telegram_chat_id FROM membre WHERE id = %s", (mid,), role=ROLE) or {}
    origp = db.fetch_one("SELECT email AS e, telegram AS t FROM preference_notification WHERE membre_id = %s", (mid,), role=ROLE)
    # Ensure both channels have a real target so a send is possible when enabled.
    db.execute("UPDATE membre SET telegram_chat_id = %s WHERE id = %s", ("ONOFF_TEST_CHAT", mid), role=ROLE)
    try:
        _set_pref(mid, True, True)
        assert {"email", "telegram"} <= set(_attempts(mid))  # ON = really sent, both channels

        _set_pref(mid, False, True)
        s = _attempts(mid)
        assert "email" not in s and "telegram" in s  # e-mail OFF = not sent; Telegram intact

        _set_pref(mid, True, False)
        s = _attempts(mid)
        assert "telegram" not in s and "email" in s  # Telegram OFF = not sent; e-mail intact
    finally:
        db.execute("UPDATE membre SET telegram_chat_id = %s WHERE id = %s", (orig.get("telegram_chat_id"), mid), role=ROLE)
        if origp is not None:
            _set_pref(mid, bool(origp["e"]), bool(origp["t"]))
        db.execute("DELETE FROM notification WHERE membre_id = %s AND type = 'test_onoff'", (mid,), role=ROLE)
