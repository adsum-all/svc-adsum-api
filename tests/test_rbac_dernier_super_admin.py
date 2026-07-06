"""Unit tests for the last-super_admin availability guard (db mocked)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import groupes
from app.schemas import UserMe


def _actor(membre_id: str | None = "actor-membre") -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role="super_admin", membre_id=membre_id)


def _mock_db(monkeypatch: pytest.MonkeyPatch, autres: int, total: int) -> None:
    def fake_fetch_one(sql: str, params: object = (), role: object = None) -> dict[str, int]:
        if "membre_groupe" in sql:
            return {"n": autres}
        return {"n": total}

    monkeypatch.setattr(groupes.db, "fetch_one", fake_fetch_one)


def test_non_super_admin_group_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_db(monkeypatch, autres=0, total=0)
    groupes._assert_super_admin_preserve("m-1", "admin", _actor())  # no raise


def test_kept_via_another_super_admin_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2 active super_admin memberships: removing one keeps them super_admin.
    _mock_db(monkeypatch, autres=2, total=1)
    groupes._assert_super_admin_preserve("m-1", "super_admin", _actor())  # no raise


def test_self_removal_of_super_admin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_db(monkeypatch, autres=1, total=5)
    with pytest.raises(HTTPException) as exc:
        groupes._assert_super_admin_preserve("me", "super_admin", _actor(membre_id="me"))
    assert exc.value.status_code == 403


def test_last_super_admin_removal_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_db(monkeypatch, autres=1, total=1)
    with pytest.raises(HTTPException) as exc:
        groupes._assert_super_admin_preserve("m-1", "super_admin", _actor())
    assert exc.value.status_code == 409


def test_demote_non_last_super_admin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Another super_admin account exists (total 2), and it is not self-removal.
    _mock_db(monkeypatch, autres=1, total=2)
    groupes._assert_super_admin_preserve("m-1", "super_admin", _actor())  # no raise
