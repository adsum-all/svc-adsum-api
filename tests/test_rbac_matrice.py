"""Unit tests for the access matrix builder (pure, no database).

The matrix screen in the back office must derive from the very mapping the server
enforces, so these tests assert the builder mirrors app.permissions_data exactly.
"""
from __future__ import annotations

from app import groupes_lecture as groupes
from app.permissions_data import CATALOGUE, permissions_du_role


def test_matrice_lists_the_full_catalogue() -> None:
    matrice = groupes._matrice_permissions()
    cles = {p["cle"] for p in matrice["permissions"]}
    assert cles == set(CATALOGUE)
    # Each permission carries the descriptive fields the UI needs, including the
    # human explanation (description) and boundary (limite).
    for p in matrice["permissions"]:
        assert set(p) == {"cle", "domaine", "libelle", "risque", "portee", "description", "limite"}
        assert p["description"] and p["limite"]  # every permission is documented


def test_matrice_domaines_are_unique_and_sorted() -> None:
    matrice = groupes._matrice_permissions()
    domaines = matrice["domaines"]
    assert domaines == sorted(set(domaines))
    assert set(domaines) == {meta["domaine"] for meta in CATALOGUE.values()}


def test_matrice_roles_mirror_the_enforced_mapping() -> None:
    matrice = groupes._matrice_permissions()
    ordre = [entry["role"] for entry in matrice["roles"]]
    assert ordre == ["membre", "controleur", "gestionnaire", "direction", "admin", "super_admin"]
    for entry in matrice["roles"]:
        assert entry["permissions"] == sorted(permissions_du_role(entry["role"]))


def test_membre_is_least_privileged_and_super_admin_most() -> None:
    matrice = groupes._matrice_permissions()
    par_role = {e["role"]: set(e["permissions"]) for e in matrice["roles"]}
    assert par_role["membre"] == {"membres.self"}
    # super_admin is a strict superset of admin (adds the two systeme permissions).
    assert par_role["admin"] < par_role["super_admin"]
    assert par_role["super_admin"] - par_role["admin"] == {"acces.systeme", "comptes.systeme"}
