"""Unit tests for the access-group hierarchy guard (no database).

These pin the anti-escalation rule that closes the critical self-promotion path:
who may grant or revoke a group that confers a given platform role.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.groupes import _assert_peut_gerer
from app.schemas import UserMe


def _actor(role: str, membre_id: str = "actor-membre") -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role=role, membre_id=membre_id)


def test_admin_cannot_grant_super_admin() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_peut_gerer(_actor("admin"), "super_admin", "cible")
    assert exc.value.status_code == 403


def test_admin_cannot_grant_admin() -> None:
    with pytest.raises(HTTPException):
        _assert_peut_gerer(_actor("admin"), "admin", "cible")


def test_admin_can_grant_gestionnaire() -> None:
    _assert_peut_gerer(_actor("admin"), "gestionnaire", "cible")


def test_super_admin_can_grant_super_admin() -> None:
    _assert_peut_gerer(_actor("super_admin"), "super_admin", "cible")


def test_super_admin_can_manage_own_access() -> None:
    # A super_admin is unrestricted, including on their own account.
    _assert_peut_gerer(_actor("super_admin", "me"), "admin", "me")


def test_admin_cannot_manage_own_access() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_peut_gerer(_actor("admin", "me"), "gestionnaire", "me")
    assert exc.value.status_code == 403


def test_gestionnaire_cannot_grant_gestionnaire() -> None:
    with pytest.raises(HTTPException):
        _assert_peut_gerer(_actor("gestionnaire"), "gestionnaire", "cible")


def test_gestionnaire_can_grant_controleur() -> None:
    _assert_peut_gerer(_actor("gestionnaire"), "controleur", "cible")
