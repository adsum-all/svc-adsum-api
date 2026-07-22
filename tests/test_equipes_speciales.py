"""Unit guards for the special-teams module: input validation, the row mapper, and
the permission wiring (catalogue entry + route mapping). No database is touched;
the DB-backed CRUD is exercised live after deployment."""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app import permissions_data
from app.equipes_speciales import EquipeIn, MembreEquipeIn, _equipe_dict


def test_membre_role_pattern_accepts_valid() -> None:
    assert MembreEquipeIn(membre_id="m-1", role="membre").role == "membre"
    assert MembreEquipeIn(membre_id="m-1", role="responsable").role == "responsable"
    # Default role is 'membre'.
    assert MembreEquipeIn(membre_id="m-1").role == "membre"


def test_membre_role_pattern_rejects_invalid() -> None:
    with pytest.raises(ValidationError):
        MembreEquipeIn(membre_id="m-1", role="chef")


def test_equipe_nom_required_and_bounded() -> None:
    with pytest.raises(ValidationError):
        EquipeIn(nom="")  # min_length=1
    with pytest.raises(ValidationError):
        EquipeIn(nom="x" * 121)  # max_length=120
    ok = EquipeIn(nom="Équipe dirigeante")
    assert ok.officielle is True and ok.ordre == 0


def test_equipe_dict_shape_and_coercion() -> None:
    row = {
        "id": uuid.uuid4(), "code": "equipe_dirigeante", "nom": "Équipe dirigeante",
        "description": None, "officielle": True, "active": True, "ordre": 1, "effectif": 5,
    }
    d = _equipe_dict(row)
    assert d["id"] == str(row["id"])
    assert d["description"] == ""  # None normalised to empty string
    assert d["officielle"] is True and d["active"] is True
    assert d["ordre"] == 1 and d["effectif"] == 5


def test_permission_declared_in_catalogue() -> None:
    assert "organisation.equipes" in permissions_data.CATALOGUE
    assert permissions_data.CATALOGUE["organisation.equipes"]["domaine"] == "organisation"


def test_equipes_routes_mapped_to_permission() -> None:
    routes = {r: p for r, p in permissions_data.ENDPOINT_PERMISSION.items() if "equipes-speciales" in r}
    assert routes, "no equipes-speciales route is mapped"
    assert all(p == "organisation.equipes" for p in routes.values()), routes
