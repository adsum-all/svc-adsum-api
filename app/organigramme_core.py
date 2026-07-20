"""Serialisation, constants and cycle detection for the organisation chart.

Extracted from ``organigramme.py`` to keep each module focused: this file holds
the pure, reusable helpers (row-to-dict serialisers, the full content read, the
allowed value sets and the hierarchical-cycle detector), while the router module
keeps the endpoints and the edit workflow.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import db

LIEN_HIERARCHIQUE = "hierarchique"
TYPES_LIEN = {"hierarchique", "coordination", "supervision", "suivi_transversal", "responsabilite_tribu", "assistance"}
STATUTS_NOEUD = {"actif", "vacant", "attente", "archive"}


def node_dict(r: dict[str, Any], masquer_photo_non_affichee: bool = False) -> dict[str, Any]:
    # When afficher_photo is false, the direction explicitly chose NOT to show the
    # person's photo. On the published/consultation read (served to every authenticated
    # member and collaborator), drop the photo URL entirely so it cannot leak; the
    # back-office editor keeps it (default False) to preview the toggle.
    afficher_photo = bool(r.get("afficher_photo"))
    photo_url = r.get("photo_url")
    if masquer_photo_non_affichee and not afficher_photo:
        photo_url = None
    return {
        "id": str(r["id"]), "cle": r.get("cle"), "type_noeud": r.get("type_noeud"),
        "nom": r.get("nom"), "sous_titre": r.get("sous_titre"),
        "membre_id": str(r["membre_id"]) if r.get("membre_id") else None,
        "membre_nom": r.get("membre_nom"),
        "fonction_cle": r.get("fonction_cle"), "categorie": r.get("categorie"),
        "unite_type": r.get("unite_type"), "unite_id": str(r["unite_id"]) if r.get("unite_id") else None,
        "effectif": r.get("effectif"), "statut": r.get("statut"),
        "pos_x": r.get("pos_x"), "pos_y": r.get("pos_y"), "ordre": r.get("ordre"),
        "couleur": r.get("couleur"), "afficher_photo": afficher_photo,
        "photo_url": photo_url, "largeur": r.get("largeur"), "hauteur": r.get("hauteur"),
    }


def link_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "source_id": str(r["source_id"]), "cible_id": str(r["cible_id"]),
        "type_lien": r.get("type_lien"), "libelle": r.get("libelle"),
    }


def version_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "libelle": r.get("libelle"), "statut": r.get("statut"),
        "note": r.get("note"), "cree_le": r.get("cree_le"), "publie_le": r.get("publie_le"),
    }


def contenu(version_id: str, role: str | None, masquer_photo_non_affichee: bool = False) -> dict[str, Any]:
    nodes = db.fetch_all(
        "SELECT n.id, n.cle, n.type_noeud, n.nom, n.sous_titre, n.membre_id, n.fonction_cle, n.categorie, n.unite_type, n.unite_id, "
        "n.effectif, n.statut, n.pos_x, n.pos_y, n.ordre, n.couleur, n.afficher_photo, n.largeur, n.hauteur, "
        "m.photo_url AS photo_url, m.nom_affiche AS membre_nom_affiche, m.prenoms AS membre_prenoms, m.nom AS membre_nom_civil "
        "FROM organisation_node n LEFT JOIN membre m ON m.id = n.membre_id "
        "WHERE n.version_id = %s AND n.actif = true ORDER BY n.ordre",
        (version_id,), role=role,
    )
    for n in nodes:
        n["membre_nom"] = n.get("membre_nom_affiche") or (
            f"{n.get('membre_prenoms') or ''} {n.get('membre_nom_civil') or ''}".strip() or None
        )
    links = db.fetch_all(
        "SELECT id, source_id, cible_id, type_lien, libelle FROM organisation_link WHERE version_id = %s AND actif = true",
        (version_id,), role=role,
    )
    return {"noeuds": [node_dict(n, masquer_photo_non_affichee) for n in nodes], "liens": [link_dict(le) for le in links]}


def aretes_hierarchiques(version_id: str, role: str | None, extra: tuple[str, str] | None = None, exclure_lien: str | None = None) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for le in db.fetch_all("SELECT id, source_id, cible_id FROM organisation_link WHERE version_id = %s AND actif = true AND type_lien = %s", (version_id, LIEN_HIERARCHIQUE), role=role):
        # When re-testing a link that is being MOVED, drop its old edge so only the new
        # endpoints are checked (otherwise the stale edge could distort the result).
        if exclure_lien and str(le["id"]) == exclure_lien:
            continue
        adj.setdefault(str(le["source_id"]), []).append(str(le["cible_id"]))
    if extra:
        adj.setdefault(extra[0], []).append(extra[1])
    return adj


def detecte_cycle(adj: dict[str, list[str]]) -> bool:
    couleur: dict[str, int] = {}

    def visite(n: str) -> bool:
        couleur[n] = 1
        for v in adj.get(n, []):
            c = couleur.get(v, 0)
            if c == 1 or (c == 0 and visite(v)):
                return True
        couleur[n] = 2
        return False

    return any(couleur.get(n, 0) == 0 and visite(n) for n in list(adj.keys()))


def cree_un_cycle(version_id: str, source_id: str, cible_id: str, role: str | None) -> bool:
    return detecte_cycle(aretes_hierarchiques(version_id, role, extra=(source_id, cible_id)))


def cree_un_cycle_maj(version_id: str, lien_id: str, source_id: str, cible_id: str, role: str | None) -> bool:
    """Cycle test for MOVING an existing hierarchical link: exclude its old edge, add the new one."""
    return detecte_cycle(aretes_hierarchiques(version_id, role, extra=(source_id, cible_id), exclure_lien=lien_id))
