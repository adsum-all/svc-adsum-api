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


def _staff_headers(client, role: str) -> dict[str, str]:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["staff"][role]
    token = connexion(client, login["email"], login["password"])
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


def test_admin_can_list_organization() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    for path in ("intendances", "coordinations", "sous-commissions", "bergers", "tribus", "doublons"):
        res = client.get(f"/api/v1/admin/{path}", headers=headers)
        assert res.status_code == 200, res.text
        assert isinstance(res.json(), list)


def test_admin_can_register_and_list_terminals() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    listing = client.get("/api/v1/admin/terminaux", headers=headers)
    assert listing.status_code == 200, listing.text
    assert isinstance(listing.json(), list)

    appareil = f"itest-{uuid.uuid4().hex[:10]}"
    created = client.post(
        "/api/v1/admin/terminaux",
        headers=headers,
        json={"nom": "Terminal de test", "appareil_id": appareil},
    )
    assert created.status_code == 201, created.text
    terminal_id = created.json()["id"]
    try:
        assert created.json()["autorise"] is True
        revoked = client.patch(
            f"/api/v1/admin/terminaux/{terminal_id}", headers=headers, json={"autorise": False}
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["autorise"] is False
    finally:
        _delete_terminal(terminal_id)


def _delete_terminal(terminal_id: str) -> None:
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM terminal WHERE id = %s", (terminal_id,))
        conn.commit()


def test_audit_journal_records_a_member_creation() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    email = f"itest-audit-{uuid.uuid4().hex[:10]}@example.com"
    created = client.post("/api/v1/admin/membres", headers=headers, json={"email": email, "nom": "Audit"})
    assert created.status_code == 201, created.text
    membre_id = created.json()["id"]
    try:
        journal = client.get("/api/v1/admin/audit", headers=headers)
        assert journal.status_code == 200, journal.text
        entries = journal.json()
        assert isinstance(entries, list)
        assert any(e["action"] == "creation_membre" and e["objet_id"] == membre_id for e in entries)
    finally:
        _delete_membre(membre_id)


def test_admin_statistics() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    res = client.get("/api/v1/admin/statistiques", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "membres_total" in body
    assert isinstance(body["par_commission"], list)
