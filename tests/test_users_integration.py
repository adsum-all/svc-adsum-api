"""Integration tests for user and rights management against the real PostgreSQL.

Skipped when ADSUM_DATABASE_URL or the provisioned accounts file is missing.
Creates a temporary account as the super admin, then removes it to keep the
database clean.
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid

import pytest

from tests.auth_reelle import entetes

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
    # Two-factor is mandatory for staff, so the plain login answers with a code
    # request and an empty token. The shared helper completes that step.
    return entetes(client, email, password)


def _delete_user(user_id: str) -> None:
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM utilisateur WHERE id = %s", (user_id,))
        conn.commit()


def test_super_admin_manages_accounts() -> None:
    creds = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["super_admin"]
    client = _client()
    headers = _headers(client, creds["email"], creds["password"])

    listing = client.get("/api/v1/admin/utilisateurs", headers=headers)
    assert listing.status_code == 200, listing.text
    assert isinstance(listing.json(), list)

    email = f"itest-user-{uuid.uuid4().hex[:10]}@example.com"
    created = client.post(
        "/api/v1/admin/utilisateurs",
        headers=headers,
        json={"email": email, "role": "controleur", "password": "Str0ngP4ssword!"},
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    try:
        assert created.json()["role"] == "controleur"
        updated = client.patch(
            f"/api/v1/admin/utilisateurs/{user_id}",
            headers=headers,
            json={"role": "gestionnaire", "actif": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["role"] == "gestionnaire"
        assert updated.json()["actif"] is False
    finally:
        _delete_user(user_id)


def _delete_users_by_email(emails: list[str]) -> None:
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM utilisateur WHERE email = ANY(%s)", (emails,))
        conn.commit()


def test_super_admin_bulk_creates_accounts() -> None:
    creds = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["super_admin"]
    client = _client()
    headers = _headers(client, creds["email"], creds["password"])
    emails = [f"itest-bulk-{uuid.uuid4().hex[:8]}@example.com" for _ in range(3)]
    comptes = [{"email": e, "password": "Str0ngP4ssword!", "role": "direction"} for e in emails]
    try:
        res = client.post("/api/v1/admin/utilisateurs/lot", headers=headers, json={"comptes": comptes})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["crees"] == 3
        # A second run reports them all as duplicates.
        again = client.post("/api/v1/admin/utilisateurs/lot", headers=headers, json={"comptes": comptes})
        assert again.json()["crees"] == 0
        assert len(again.json()["doublons"]) == 3
    finally:
        _delete_users_by_email(emails)


def test_admin_cannot_create_account() -> None:
    creds = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["staff"]["admin"]
    client = _client()
    headers = _headers(client, creds["email"], creds["password"])
    res = client.post(
        "/api/v1/admin/utilisateurs",
        headers=headers,
        json={"email": f"x-{uuid.uuid4().hex[:8]}@example.com", "role": "controleur", "password": "Str0ngP4ss!"},
    )
    assert res.status_code == 403, res.text
