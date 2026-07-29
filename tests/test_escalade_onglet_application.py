"""Granting a group from an application tab must obey the same least-privilege guard.

The direct route refused an administrator who tried to hand themselves a group that
grants a higher role. The per-application route did the same write without asking, so
the tab was a way round the guard: an admin could take the group that grants
super_admin, and since a tagged membership no longer raises the account role, they
would exercise that set without even appearing as platform staff.

These tests pin the guard on both shapes: the self-grant and the higher-role grant.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.groupes_roles import _assert_peut_gerer
from app.schemas import UserMe


def _acteur(role: str, membre_id: str = "11111111-1111-1111-1111-111111111111") -> UserMe:
    return UserMe(id="99999999-9999-9999-9999-999999999999", email="a@b.c", role=role, membre_id=membre_id)


def test_un_admin_ne_peut_pas_se_donner_un_groupe() -> None:
    """Self-elevation is refused whatever the granted role, even a lower one."""
    acteur = _acteur("admin")
    with pytest.raises(HTTPException) as exc:
        _assert_peut_gerer(acteur, "gestionnaire", acteur.membre_id or "")
    assert exc.value.status_code == 403


def test_un_admin_ne_peut_pas_accorder_super_admin() -> None:
    """The group that grants a rank at or above the actor's own is refused."""
    acteur = _acteur("admin")
    with pytest.raises(HTTPException) as exc:
        _assert_peut_gerer(acteur, "super_admin", "22222222-2222-2222-2222-222222222222")
    assert exc.value.status_code == 403


def test_un_admin_ne_peut_pas_accorder_admin() -> None:
    """Equal rank is refused too: the guard is strictly below, not below or equal."""
    acteur = _acteur("admin")
    with pytest.raises(HTTPException) as exc:
        _assert_peut_gerer(acteur, "admin", "22222222-2222-2222-2222-222222222222")
    assert exc.value.status_code == 403


def test_un_admin_peut_accorder_un_role_inferieur() -> None:
    """Delegation downwards stays possible: the guard bounds, it does not block."""
    _assert_peut_gerer(_acteur("admin"), "gestionnaire", "22222222-2222-2222-2222-222222222222")


def test_un_super_admin_peut_tout_accorder() -> None:
    """A super_admin manages any group, including their own access."""
    acteur = _acteur("super_admin")
    _assert_peut_gerer(acteur, "super_admin", acteur.membre_id or "")


def test_la_route_par_application_appelle_la_garde() -> None:
    """The per-application route must call the guard, not merely exist beside it.

    Asserted on the source rather than through a live request: exercising the route
    would need a real member, a real application and a real group, and the point here
    is that the call site exists at all, which is exactly what was missing.
    """
    import inspect

    from app import applications

    source = inspect.getsource(applications.definir_groupe_application)
    assert "_assert_peut_gerer" in source, "la garde de moindre privilège a disparu de la route par application"
    assert "_assert_peut_accorder_permissions" in source, (
        "la garde des permissions a disparu de la route par application"
    )
