"""Unit tests for the permission-group least-privilege guard (F2).

A 'permissions' group must never let a non super_admin hand out a permission they
do not hold, nor any 'critique' permission. Unknown keys are rejected outright.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import groupes, permissions_rbac
from app.schemas import UserMe


def _actor(role: str) -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role=role, membre_id="m-1")


def _hold(monkeypatch: pytest.MonkeyPatch, perms: set[str]) -> None:
    monkeypatch.setattr(permissions_rbac, "permissions_effectives", lambda user: frozenset(perms))


def test_super_admin_can_grant_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    # No hold restriction applies to super_admin, even a critical permission.
    groupes._assert_peut_accorder_permissions(["acces.administrer", "membres.gerer"], _actor("super_admin"))


def test_unknown_permission_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _hold(monkeypatch, {"membres.gerer"})
    with pytest.raises(HTTPException) as exc:
        groupes._assert_peut_accorder_permissions(["membres.inexistante"], _actor("admin"))
    assert exc.value.status_code == 400


def test_non_super_admin_cannot_grant_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    # admin holds acces.administrer (critique) but may not put it in a group.
    _hold(monkeypatch, {"acces.administrer", "membres.gerer"})
    with pytest.raises(HTTPException) as exc:
        groupes._assert_peut_accorder_permissions(["acces.administrer"], _actor("admin"))
    assert exc.value.status_code == 403


def test_non_super_admin_cannot_grant_unheld(monkeypatch: pytest.MonkeyPatch) -> None:
    _hold(monkeypatch, {"membres.consulter"})
    with pytest.raises(HTTPException) as exc:
        groupes._assert_peut_accorder_permissions(["evenements.gerer"], _actor("gestionnaire"))
    assert exc.value.status_code == 403


def test_non_super_admin_can_grant_held_non_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    _hold(monkeypatch, {"membres.gerer", "evenements.gerer"})
    groupes._assert_peut_accorder_permissions(["membres.gerer", "evenements.gerer"], _actor("gestionnaire"))
