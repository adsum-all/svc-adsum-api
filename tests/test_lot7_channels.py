"""LOT 7 delivery channels: SMS number normalisation, config-gated no-ops for SMS
and WhatsApp, and per-type WhatsApp template resolution.

These are pure or config-gated helpers: they must never send nor raise when a
provider or template is not configured, so the fan-out is never blocked.
"""
from __future__ import annotations

import pytest

from app import channels, notifications
from app.config import settings


@pytest.mark.parametrize(
    ("indicatif", "telephone", "attendu"),
    [
        ("+225", "0700000000", "225700000000"),  # national trunk 0 dropped
        ("225", "0700000000", "225700000000"),
        ("+33", "612345678", "33612345678"),
        ("", "0612345678", "0612345678"),  # no dialing code: kept as-is
        (None, None, ""),
        ("+225", "12", ""),  # too short -> unusable
    ],
)
def test_sms_numero_normalisation(indicatif: object, telephone: object, attendu: str) -> None:
    assert channels._sms_numero(indicatif, telephone) == attendu


def test_send_sms_noop_when_provider_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "")
    assert channels.send_sms("2250700000000", "hello") is False


def test_send_sms_noop_when_provider_unknown_or_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sms_provider", "brevo")
    monkeypatch.setattr(settings, "email_api_key", "")
    assert channels.send_sms("2250700000000", "hello") is False


def test_send_whatsapp_noop_without_resolved_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "whatsapp_token", "t")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    monkeypatch.setattr(settings, "whatsapp_template_anniversaire", "")
    # No fallback template and none passed: a clean no-op, never a crash.
    assert channels.send_whatsapp("2250700000000", ["a"], None) is False


def test_whatsapp_template_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "whatsapp_template_anniversaire", "anniv_tpl")
    monkeypatch.setattr(settings, "whatsapp_template_collab", "collab_tpl")
    assert notifications._whatsapp_template_for("anniversaire") == "anniv_tpl"
    assert notifications._whatsapp_template_for("collab_mention") == "collab_tpl"
    assert notifications._whatsapp_template_for("collab_demande") == "collab_tpl"
    assert notifications._whatsapp_template_for("compte_bloque") is None


def test_whatsapp_template_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "whatsapp_template_collab", "")
    assert notifications._whatsapp_template_for("collab_publication") is None
