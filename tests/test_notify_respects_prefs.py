"""Regression: member confirmations (_notify) must honor the channel On/Off preferences.

The previous _notify sent the e-mail directly, bypassing preferences, so a member who
turned e-mail off kept receiving these e-mails. It now routes through channels.dispatch with
the member's allowed channels, so e-mail is delivered only when the member enabled it.
"""
from __future__ import annotations

from app import channels, membres
from app import notifications as notif


def test_notify_routes_through_dispatch_with_allowed_channels(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(membres.db, "fetch_one", lambda *a, **k: {"sensibilite": "operationnel"})
    # This member disabled e-mail: only Telegram is allowed for this type.
    monkeypatch.setattr(notif, "canaux_autorises", lambda *a, **k: {"telegram"})

    def fake_dispatch(mid, role, message, whatsapp_params=None, critique=False, canaux_autorises=None):
        captured.update(type=message.type_notif, critique=critique, canaux=canaux_autorises)
        return ["in-app"]

    monkeypatch.setattr(channels, "dispatch", fake_dispatch)

    membres._notify("M", "membre", "document", "Document recu", "Votre document a bien ete recu.")

    # Routed through dispatch, non-critical, with e-mail excluded from the allowed channels.
    assert captured["type"] == "document"
    assert captured["critique"] is False
    assert "email" not in captured["canaux"]
    assert "telegram" in captured["canaux"]


def test_notify_marks_critical_types(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(membres.db, "fetch_one", lambda *a, **k: {"sensibilite": "critique"})
    monkeypatch.setattr(notif, "canaux_autorises", lambda *a, **k: None)
    monkeypatch.setattr(channels, "dispatch", lambda *a, **k: captured.update(critique=k.get("critique")) or ["in-app"])
    membres._notify("M", "membre", "otp", "T", "C")
    assert captured["critique"] is True
