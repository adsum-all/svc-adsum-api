"""Unit tests for GET /membres/me/permissions (no database).

A UserMe without a linked membre_id resolves permissions from the role cache only
(permissions_effectives returns early before any DB call), so the endpoint logic
is testable in isolation.
"""
from __future__ import annotations

from app import membres
from app.schemas import UserMe


def _user(role: str) -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role=role, membre_id=None)


def test_plain_membre_has_no_back_office_access() -> None:
    out = membres.my_permissions(_user("membre"))
    assert out["role"] == "membre"
    assert out["permissions"] == ["membres.self"]
    assert out["acces_back_office"] is False


def test_gestionnaire_has_back_office_access() -> None:
    out = membres.my_permissions(_user("gestionnaire"))
    assert out["acces_back_office"] is True
    assert "membres.gerer" in out["permissions"]


def test_permissions_are_sorted_and_deduplicated() -> None:
    out = membres.my_permissions(_user("admin"))
    perms = out["permissions"]
    assert perms == sorted(set(perms))
