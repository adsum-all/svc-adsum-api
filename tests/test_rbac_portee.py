"""Regression tests: a scoped membership never grants a global permission (F1).

A scoped group (coordination/intendance/commission/tribu) must grant only a
bounded pilotage access through resolve_scope, never a global permission. Before
the fix, permissions_effectives unioned every membership regardless of scope,
leaking a scoped group's whole role across the base.
"""
from __future__ import annotations

import pytest

from app import permissions_rbac as rbac
from app.schemas import UserMe


def _user(role: str = "membre") -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role=role, membre_id="m-1")


def test_query_filters_global_memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch_all(sql: str, params: object = (), role: object = None) -> list[dict[str, object]]:
        captured["sql"] = sql
        return []

    monkeypatch.setattr(rbac.db, "fetch_all", fake_fetch_all)
    rbac.permissions_effectives(_user())
    assert "mg.portee_type = 'global'" in captured["sql"]


def test_scoped_only_member_has_no_global_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    # The DB honors the WHERE portee_type = 'global', so a scoped-only member
    # returns no rows: the effective set is just the role cache baseline.
    monkeypatch.setattr(rbac.db, "fetch_all", lambda *a, **k: [])
    perms = rbac.permissions_effectives(_user("membre"))
    assert perms == frozenset({"membres.self"})
    assert "membres.gerer" not in perms


def test_global_permission_group_adds_its_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"mode": "permissions", "role_accorde": "membre", "permission": "inscriptions.gerer"}]
    monkeypatch.setattr(rbac.db, "fetch_all", lambda *a, **k: rows)
    perms = rbac.permissions_effectives(_user("membre"))
    assert "inscriptions.gerer" in perms
    assert "membres.self" in perms


def test_global_role_group_adds_the_role_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"mode": "role", "role_accorde": "gestionnaire", "permission": None}]
    monkeypatch.setattr(rbac.db, "fetch_all", lambda *a, **k: rows)
    perms = rbac.permissions_effectives(_user("membre"))
    assert "membres.gerer" in perms
