"""Integration tests for the control endpoints against the real PostgreSQL.

Skipped automatically when ADSUM_DATABASE_URL or the provisioned accounts file is
not available (for example in CI), so it never blocks the pipeline. The flow:
a member issues a QR token, then a controller verifies it and checks the member
in at a real event created by an admin.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ACCOUNTS = pathlib.Path(__file__).resolve().parents[4] / ".secret" / "adsum-accounts.json"

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL") or not ACCOUNTS.exists(),
    reason="real database or provisioned accounts not available",
)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _headers(client, email: str, password: str) -> dict[str, str]:
    ok = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert ok.status_code == 200, ok.text
    return {"Authorization": f"Bearer {ok.json()['access_token']}"}


def _staff_headers(client, role: str) -> dict[str, str]:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["staff"][role]
    return _headers(client, login["email"], login["password"])


def _member_headers(client) -> dict[str, str]:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["membre_login"]
    return _headers(client, login["email"], login["password"])


def _set_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use a deterministic ephemeral key so issue and verify share the same key
    # within the test process, regardless of the environment configuration.
    from app.config import settings

    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(settings, "qr_signing_key", base64.urlsafe_b64encode(key.private_bytes_raw()).decode())
    monkeypatch.setattr(settings, "qr_public_key", "")


def _create_event(client) -> str:
    headers = _staff_headers(client, "admin")
    now = datetime.now(UTC)
    res = client.post(
        "/api/v1/admin/evenements",
        headers=headers,
        json={
            "titre": "Integration control session",
            "debut": now.isoformat(),
            "fin": (now + timedelta(hours=3)).isoformat(),
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _cleanup_event(evenement_id: str) -> None:
    """Delete a test event and its presences, to keep the database clean."""
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM presence WHERE evenement_id = %s", (evenement_id,))
        cur.execute("DELETE FROM evenement WHERE id = %s", (evenement_id,))
        conn.commit()


def test_verify_and_checkin_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_signing_key(monkeypatch)
    client = _client()

    qr = client.get("/api/v1/membres/me/qr", headers=_member_headers(client))
    assert qr.status_code == 200, qr.text
    token = qr.json()["token"]

    control_headers = _staff_headers(client, "controleur")
    verified = client.post("/api/v1/controle/verify", headers=control_headers, json={"token": token})
    assert verified.status_code == 200, verified.text
    assert verified.json()["valid"] is True
    assert verified.json()["matricule"]

    evenement_id = _create_event(client)
    try:
        checkin = client.post(
            "/api/v1/controle/checkin",
            headers=control_headers,
            json={"token": token, "evenement_id": evenement_id},
        )
        assert checkin.status_code == 200, checkin.text
        first = checkin.json()
        assert first["deja_present"] is False
        assert first["evenement_id"] == evenement_id
        assert first["membre"]["matricule"]

        repeat = client.post(
            "/api/v1/controle/checkin",
            headers=control_headers,
            json={"token": token, "evenement_id": evenement_id},
        )
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["deja_present"] is True

        # Exit mode: a second scan records the departure.
        out = client.post(
            "/api/v1/controle/checkout",
            headers=control_headers,
            json={"token": token, "evenement_id": evenement_id},
        )
        assert out.status_code == 200, out.text
        assert out.json()["depart"] is not None
        assert out.json()["deja_sorti"] is False
    finally:
        _cleanup_event(evenement_id)


def test_directory_lists_members() -> None:
    client = _client()
    headers = _staff_headers(client, "controleur")
    listing = client.get("/api/v1/controle/membres", headers=headers)
    assert listing.status_code == 200, listing.text
    assert isinstance(listing.json(), list)
    filtered = client.get("/api/v1/controle/membres", headers=headers, params={"q": "ADS-000001"})
    assert filtered.status_code == 200, filtered.text


def test_checkin_rejects_an_invalid_token() -> None:
    client = _client()
    control_headers = _staff_headers(client, "controleur")
    res = client.post(
        "/api/v1/controle/checkin",
        headers=control_headers,
        json={"token": "ADSUM1.garbage.signature", "evenement_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert res.status_code == 422, res.text


def test_control_events_listing_is_a_list() -> None:
    client = _client()
    res = client.get("/api/v1/controle/evenements", headers=_staff_headers(client, "controleur"))
    assert res.status_code == 200, res.text
    assert isinstance(res.json(), list)
