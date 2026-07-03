"""Real login integration test against the actual PostgreSQL.

Skipped automatically when ADSUM_DATABASE_URL or the provisioned accounts file is
not available (for example in CI), so it never blocks the pipeline. Run locally
with the database reachable to prove the real authentication loop.
"""
from __future__ import annotations

import json
import os
import pathlib

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


def test_super_admin_can_login_and_read_me() -> None:
    creds = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["super_admin"]
    client = _client()

    bad = client.post("/api/v1/auth/login", json={"email": creds["email"], "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/api/v1/auth/login", json={"email": creds["email"], "password": creds["password"]})
    assert ok.status_code == 200, ok.text
    token = ok.json()["access_token"]
    assert ok.json()["role"] == "super_admin"

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == creds["email"]
    assert me.json()["role"] == "super_admin"


def test_member_account_can_login() -> None:
    login = json.loads(ACCOUNTS.read_text(encoding="utf-8"))["membre_login"]
    client = _client()
    ok = client.post("/api/v1/auth/login", json={"email": login["email"], "password": login["password"]})
    assert ok.status_code == 200, ok.text
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})
    assert me.status_code == 200
    assert me.json()["membre_id"] is not None
