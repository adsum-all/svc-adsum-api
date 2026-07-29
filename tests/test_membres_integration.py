"""Integration tests for the member endpoints against the real PostgreSQL.

Skipped automatically when ADSUM_DATABASE_URL or the provisioned accounts file is
not available (for example in CI), so it never blocks the pipeline. Run locally
with the database reachable to prove the real member loop.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.auth_reelle import connexion

ACCOUNTS = pathlib.Path(__file__).resolve().parents[4] / ".secret" / "adsum-accounts.json"

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL") or not ACCOUNTS.exists(),
    reason="real database or provisioned accounts not available",
)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _member_headers(client) -> dict[str, str]:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["membre_login"]
    token = connexion(client, login["email"], login["password"])
    return {"Authorization": f"Bearer {token}"}


def test_member_profile_is_returned() -> None:
    client = _client()
    res = client.get("/api/v1/membres/me", headers=_member_headers(client))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["matricule"] == "ADS-000001"
    assert body["statut"] == "actif"


def test_member_events_and_history_are_lists() -> None:
    client = _client()
    headers = _member_headers(client)
    events = client.get("/api/v1/membres/me/evenements", headers=headers)
    assert events.status_code == 200, events.text
    assert isinstance(events.json(), list)
    history = client.get("/api/v1/membres/me/historique", headers=headers)
    assert history.status_code == 200, history.text
    assert isinstance(history.json(), list)


def test_member_notifications_is_a_list() -> None:
    client = _client()
    res = client.get("/api/v1/membres/me/notifications", headers=_member_headers(client))
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)


def test_member_recensement() -> None:
    client = _client()
    headers = _member_headers(client)
    census = client.get("/api/v1/membres/me/recensement", headers=headers)
    assert census.status_code == 200, census.text
    body = census.json()
    if body is not None:
        assert body["ouvert"] is True
        submitted = client.post(
            "/api/v1/membres/me/recensement",
            headers=headers,
            json={"confirme_engagement": True, "infos_a_jour": True, "reaccepte_engagements": True},
        )
        assert submitted.status_code == 201, submitted.text


def test_member_qr_is_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        settings, "qr_signing_key", base64.urlsafe_b64encode(key.private_bytes_raw()).decode()
    )
    client = _client()
    res = client.get("/api/v1/membres/me/qr", headers=_member_headers(client))
    assert res.status_code == 200, res.text
    assert res.json()["token"].startswith("ADSUM1.")
    assert res.json()["expires_at"] > res.json()["issued_at"]
