"""Scenario matrix for the central organisational identity resolver.

Covers every cumulative combination of the four attribution categories (title,
special function, function, particular function) and the display precedence
special function > title > function > particular > civil name. These tests lock
the salutation contract before any notification change reaches production.
"""
from __future__ import annotations

from app.identite import resoudre_identite

CIVIL = "Jean DUPONT"


def _titre():
    return dict(est_berger=True, nom_pastoral="David")


def _no_titre():
    return dict(est_berger=False, nom_pastoral=None)


def _fn(categorie, libelle, abreviation=None, perimetre=None):
    return {"categorie": categorie, "libelle": libelle, "abreviation": abreviation, "perimetre": perimetre}


def _resolve(fonctions, **titre):
    base = dict(genre="homme", prenoms="Jean", nom_civil=CIVIL, fonctions=fonctions)
    base.update(titre or _no_titre())
    return resoudre_identite(**base)


def test_titre_seul():
    r = _resolve([], **_titre())
    assert r["appellation"] == "Berger David"
    assert r["categorie_principale"] == "titre"


def test_fonction_speciale_seule():
    r = _resolve([_fn("fonction_speciale", "Modérateur")])
    assert r["appellation"] == "Modérateur"
    assert r["categorie_principale"] == "fonction_speciale"


def test_fonction_seule_utilise_abreviation():
    r = _resolve([_fn("fonction", "Responsable", abreviation="Resp.")])
    assert r["appellation"] == "Resp. Jean"
    assert r["categorie_principale"] == "fonction"


def test_titre_et_fonction_le_titre_prime():
    r = _resolve([_fn("fonction", "Responsable", abreviation="Resp.")], **_titre())
    assert r["appellation"] == "Berger David"
    assert r["categorie_principale"] == "titre"


def test_fonction_speciale_et_titre():
    r = _resolve([_fn("fonction_speciale", "Modérateur")], **_titre())
    assert r["appellation"] == "Modérateur (Berger David)"
    assert r["categorie_principale"] == "fonction_speciale"


def test_fonction_speciale_et_fonction_la_speciale_prime():
    r = _resolve([_fn("fonction", "Responsable", abreviation="Resp."), _fn("fonction_speciale", "Fondateur")])
    assert r["appellation"] == "Fondateur"


def test_titre_speciale_et_plusieurs_fonctions():
    fns = [
        _fn("fonction_speciale", "Modérateur"),
        _fn("fonction", "Coordinateur", abreviation="Coord."),
        _fn("fonction", "Responsable", abreviation="Resp."),
    ]
    r = _resolve(fns, **_titre())
    assert r["appellation"] == "Modérateur (Berger David)"
    # Detail keeps every attribution (title first, then functions).
    assert "Berger David" in r["detail"]
    assert "Modérateur" in r["detail"]
    assert "Coordinateur" in r["detail"]


def test_plusieurs_fonctions_seules_premiere_gagne():
    fns = [_fn("fonction", "Coordinateur", abreviation="Coord."), _fn("fonction", "Responsable", abreviation="Resp.")]
    r = _resolve(fns)
    assert r["appellation"] == "Coord. Jean"


def test_aucune_attribution_nom_civil():
    r = _resolve([])
    assert r["appellation"] == "Jean"
    assert r["categorie_principale"] == "civil"


def test_fonction_particuliere_avec_perimetre():
    r = _resolve([_fn("fonction_particuliere", "Patriarche", perimetre="Tribu de Juda")])
    assert r["appellation"] == "Patriarche Jean"
    assert r["formel"] == "Patriarche Jean DUPONT (Tribu de Juda)"
    assert r["anniversaire"] == "Patriarche Jean DUPONT (Tribu de Juda)"


def test_anniversaire_fonction_speciale_avec_titre():
    r = _resolve([_fn("fonction_speciale", "Modérateur")], **_titre())
    assert r["anniversaire"] == "Modérateur (Berger David)"


def test_anniversaire_fonction_utilise_nom_complet():
    r = _resolve([_fn("fonction", "Responsable de commission", abreviation="Resp.", perimetre="Commission EDEN")])
    assert r["anniversaire"] == "Resp. Jean DUPONT"
    # Formal view keeps the full label and the scope.
    assert r["formel"] == "Responsable de commission Jean DUPONT (Commission EDEN)"


def test_bergere_feminin():
    r = resoudre_identite(genre="femme", prenoms="Marie", nom_civil="Marie CURIE",
                          est_berger=True, nom_pastoral="Marie de Jésus", fonctions=[])
    assert r["appellation"] == "Bergère Marie de Jésus"
