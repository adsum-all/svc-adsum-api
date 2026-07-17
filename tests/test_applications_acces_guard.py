"""Unit tests for the access-governance write guard (audit finding 1).

The write endpoints of the two-step access model (definir_acces, definir_groupe_application,
definir_role) guard on the permission acces.administrer, but the underlying tables
(membre_application_acces, membre_groupe) accept writes only from an admin base role at the
RLS level (super_admin/admin). Without the guard, a non-admin base role holding
acces.administrer by delegation would pass the permission guard, yet its DELETE would be
silently filtered by RLS (zero row) while the endpoint returned ok. The guard mirrors the
RLS restriction so the failure is loud and consistent.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.permissions_rbac import ROLES_ECRITURE_ACCES, assert_role_ecriture_acces
from app.schemas import UserMe


def _actor(role: str) -> UserMe:
    return UserMe(id="u-1", email="a@example.com", role=role, membre_id="m-1")


@pytest.mark.parametrize("role", ["super_admin", "admin"])
def test_admin_roles_are_allowed(role: str) -> None:
    # Must not raise: these are exactly the roles the RLS write policy accepts.
    assert_role_ecriture_acces(_actor(role))


@pytest.mark.parametrize("role", ["gestionnaire", "controleur", "direction", "membre"])
def test_non_admin_roles_are_rejected_with_403(role: str) -> None:
    with pytest.raises(HTTPException) as exc:
        assert_role_ecriture_acces(_actor(role))
    assert exc.value.status_code == 403


def test_guard_roles_match_rls_write_roles() -> None:
    # The guard must mirror the RLS write policy of migrations 0068/0134 exactly, so the
    # endpoint and the database agree on who may write the access-governance tables.
    assert ROLES_ECRITURE_ACCES == ("super_admin", "admin")
