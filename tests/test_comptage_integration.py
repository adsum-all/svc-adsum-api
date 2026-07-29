"""Integration tests for volet B counting and public self-service.

Skipped when ADSUM_DATABASE_URL or the provisioned accounts file is missing.
Creates a volet B event, counts members and non-members, and cleans up.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import UTC, datetime, timedelta

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


def _cleanup(evenement_id: str) -> None:
    import psycopg

    dsn = os.environ["ADSUM_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM comptage_volet_b WHERE evenement_id = %s", (evenement_id,))
        cur.execute("DELETE FROM evenement WHERE id = %s", (evenement_id,))
        conn.commit()


def test_volet_b_counting_and_public_self_service() -> None:
    client = _client()
    headers = _staff_headers(client, "admin")
    now = datetime.now(UTC)
    event = client.post(
        "/api/v1/admin/evenements",
        headers=headers,
        json={"titre": "Grand rassemblement test", "volet": "B", "debut": now.isoformat(),
              "fin": (now + timedelta(hours=4)).isoformat()},
    )
    assert event.status_code == 201, event.text
    eid = event.json()["id"]
    try:
        added = client.post(
            "/api/v1/admin/comptage",
            headers=headers,
            json={"evenement_id": eid, "segment": "Adultes", "total_anonyme": 120},
        )
        assert added.status_code == 201, added.text
        assert added.json()["non_membres"] == 120

        # Anonymous self-service, no authentication.
        pub = client.post(f"/api/v1/public/presence/{eid}")
        assert pub.status_code == 201, pub.text

        resume = client.get(f"/api/v1/admin/comptage/{eid}", headers=headers)
        assert resume.status_code == 200, resume.text
        assert resume.json()["non_membres"] == 121
        assert resume.json()["total_participants"] == resume.json()["membres_scannes"] + 121

        info = client.get(f"/api/v1/public/evenement/{eid}")
        assert info.status_code == 200, info.text
        assert info.json()["titre"] == "Grand rassemblement test"
    finally:
        _cleanup(eid)
