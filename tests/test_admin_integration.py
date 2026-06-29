"""Integration tests for the admin endpoints against the real PostgreSQL.

Skipped automatically when ADSUM_DATABASE_URL or the provisioned accounts file is
not available (for example in CI), so it never blocks the pipeline. Run locally
with the database reachable to prove the real back-office loop.
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid

import pytest

ACCOUNTS = pathlib.Path(__file__).resolve().parents[4] / ".secret" / "adsum-accounts.json"

pytestmark = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL") or not ACCOUNTS.exists(),
    reason="real database or provisioned accounts not available",
)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _staff_headers(client, role: str) -> dict[str, str]:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["staff"][role]
    ok = client.post("/api/v1/auth/login", json={"email": login["email"], "password": login["password"]})
    assert ok.status_code == 200, ok.text
    return {"Authorization": f"Bearer {ok.json()['access_token']}"}


def _delete_membre(membre_id: str) -> None:
    """Remove a member created by a test, to keep the database clean."""
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM membre WHERE id = %s", (membre_id,))
        conn.commit()


def test_admin_can_create_list_and_get_a_member() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    email = f"itest-{uuid.uuid4().hex[:12]}@example.com"

    created = client.post("/api/v1/admin/membres", headers=headers, json={"email": email, "nom": "Itest"})
    assert created.status_code == 201, created.text
    body = created.json()
    membre_id = body["id"]
    try:
        assert body["matricule"].startswith("ADS-")
        assert body["statut"] == "actif"

        listing = client.get("/api/v1/admin/membres", headers=headers, params={"q": "Itest"})
        assert listing.status_code == 200, listing.text
        assert any(m["id"] == membre_id for m in listing.json())

        detail = client.get(f"/api/v1/admin/membres/{membre_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["email"] == email
    finally:
        _delete_membre(membre_id)


def test_controleur_cannot_create_a_member() -> None:
    client = _client()
    headers = _staff_headers(client, "controleur")
    res = client.post(
        "/api/v1/admin/membres",
        headers=headers,
        json={"email": f"forbidden-{uuid.uuid4().hex[:8]}@example.com"},
    )
    assert res.status_code == 403, res.text


def test_admin_can_list_commissions_and_events() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    commissions = client.get("/api/v1/admin/commissions", headers=headers)
    assert commissions.status_code == 200, commissions.text
    assert isinstance(commissions.json(), list)
    events = client.get("/api/v1/admin/evenements", headers=headers)
    assert events.status_code == 200, events.text
    assert isinstance(events.json(), list)
