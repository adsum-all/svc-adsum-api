"""Organisation chart module: versioned, editable, publishable hierarchy.

Draft/publish workflow over the ``organisation_*`` tables (migration 0150). A draft
is first built from the real data (``organigramme_builder``), edited node by node
and link by link, validated (no hierarchical cycle), then published. Members read
the single published version and their own upward hierarchy. Every mutation is
recorded in ``organisation_changelog``.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import (
    audit,
    db,
    fonctions_membre,
    hierarchie_membre,
    organigramme_builder,
    organigramme_coherence,
    organigramme_stats,
)
from . import organigramme_core as oc
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["organigramme"])



def _log(version_id: str | None, acteur: str, action: str, cible_type: str | None, cible_id: str | None, avant: Any, apres: Any) -> None:
    db.execute(
        "INSERT INTO organisation_changelog (version_id, acteur_id, action, cible_type, cible_id, avant, apres) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
        (version_id, acteur, action, cible_type, cible_id,
         None if avant is None else json.dumps(avant, default=str),
         None if apres is None else json.dumps(apres, default=str)),
    )


def _version_ou_404(version_id: str, role: str | None) -> dict[str, Any]:
    row = db.fetch_one("SELECT id, libelle, statut, note, cree_le, publie_le FROM organisation_version WHERE id = %s", (version_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="version inconnue")
    return row


def _brouillon_ou_409(version: dict[str, Any]) -> None:
    if version.get("statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")


# --- Payloads ---------------------------------------------------------------

class VersionIn(BaseModel):
    libelle: str = Field(min_length=1, max_length=120)
    depuis: str | None = Field(default=None, pattern="^(reel|publie)?$")
    note: str | None = Field(default=None, max_length=500)


class NodeIn(BaseModel):
    nom: str = Field(min_length=1, max_length=160)
    type_noeud: str = Field(default="structure", pattern="^(personne|structure|groupe|separateur|note|zone)$")
    sous_titre: str | None = Field(default=None, max_length=200)
    membre_id: str | None = None
    fonction_cle: str | None = Field(default=None, max_length=40)
    categorie: str | None = Field(default=None, max_length=40)
    unite_type: str | None = Field(default=None, max_length=40)
    unite_id: str | None = None
    statut: str = Field(default="actif")
    pos_x: float | None = None
    pos_y: float | None = None
    couleur: str | None = Field(default=None, max_length=32)
    afficher_photo: bool = False
    largeur: float | None = None
    hauteur: float | None = None


class NodePatch(BaseModel):
    nom: str | None = Field(default=None, max_length=160)
    sous_titre: str | None = Field(default=None, max_length=200)
    membre_id: str | None = None
    fonction_cle: str | None = Field(default=None, max_length=40)
    categorie: str | None = Field(default=None, max_length=40)
    unite_type: str | None = Field(default=None, max_length=40)
    unite_id: str | None = None
    statut: str | None = None
    pos_x: float | None = None
    pos_y: float | None = None
    couleur: str | None = Field(default=None, max_length=32)
    afficher_photo: bool | None = None
    largeur: float | None = None
    hauteur: float | None = None


class AffectationIn(BaseModel):
    # Assign a member to a node. When appliquer_au_profil is true and the node
    # carries a function, the assignment is ALSO written to the member's profile
    # (confirmed function, or the est_berger consecration flag for a title), so the
    # chart is a real assignment tool, not just a drawing.
    membre_id: str | None = None
    appliquer_au_profil: bool = False


class LinkIn(BaseModel):
    source_id: str
    cible_id: str
    type_lien: str = Field(default="hierarchique")
    libelle: str | None = Field(default=None, max_length=120)


class LinkPatch(BaseModel):
    # Every field is optional: only those explicitly present in the request are
    # changed (tracked via model_fields_set), so an endpoint can be re-attached
    # (source/cible), re-typed or re-labelled independently, without deleting it.
    source_id: str | None = None
    cible_id: str | None = None
    type_lien: str | None = None
    libelle: str | None = Field(default=None, max_length=120)


# --- Version lifecycle ------------------------------------------------------

@router.get("/admin/organigramme/versions")
def list_versions(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT id, libelle, statut, note, cree_le, publie_le FROM organisation_version ORDER BY cree_le DESC", (), role=user.role)
    return [oc.version_dict(r) for r in rows]


@router.post("/admin/organigramme/versions", status_code=status.HTTP_201_CREATED)
def create_version(payload: VersionIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    row = db.execute(
        "INSERT INTO organisation_version (libelle, statut, note, cree_par) VALUES (%s, 'brouillon', %s, %s) RETURNING id",
        (payload.libelle, payload.note, user.id), role=user.role,
    )
    vid = str(row["id"])
    resume: dict[str, int] = {}
    if payload.depuis == "reel":
        resume = organigramme_builder.construire(vid, user.role)
    elif payload.depuis == "publie":
        resume = _cloner_publiee(vid, user.role)
    _log(vid, user.id, "creation_version", "version", vid, None, {"libelle": payload.libelle, "depuis": payload.depuis, **resume})
    return {"id": vid, "resume": resume}


def _cloner_publiee(vid: str, role: str | None) -> dict[str, int]:
    """Copy the published version's nodes and links into a fresh draft, preserving
    the graph by mapping each old node id to its new one."""
    pub = db.fetch_one("SELECT id FROM organisation_version WHERE statut = 'publie'", (), role=role)
    if not pub:
        return {"noeuds": 0}
    src = str(pub["id"])
    mapping: dict[str, str] = {}
    for n in db.fetch_all("SELECT * FROM organisation_node WHERE version_id = %s AND actif = true", (src,), role=role):
        new = db.execute(
            "INSERT INTO organisation_node (version_id, cle, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, effectif, statut, pos_x, pos_y, ordre, meta) "
            "SELECT %s, cle, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, effectif, statut, pos_x, pos_y, ordre, meta FROM organisation_node WHERE id = %s RETURNING id",
            (vid, n["id"]), role=role,
        )
        mapping[str(n["id"])] = str(new["id"])
    for le in db.fetch_all("SELECT * FROM organisation_link WHERE version_id = %s AND actif = true", (src,), role=role):
        s, c = mapping.get(str(le["source_id"])), mapping.get(str(le["cible_id"]))
        if s and c:
            db.execute("INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES (%s, %s, %s, %s, %s)",
                       (vid, s, c, le["type_lien"], le.get("libelle")), role=role)
    return {"noeuds": len(mapping)}


@router.get("/admin/organigramme/versions/{version_id}")
def get_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> dict[str, Any]:
    version = _version_ou_404(version_id, user.role)
    return {"version": oc.version_dict(version), **oc.contenu(version_id, user.role)}


@router.post("/admin/organigramme/versions/{version_id}/construire")
def construire_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    """(Re)build a draft from the real data: clears its nodes/links then rebuilds."""
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    db.execute("DELETE FROM organisation_link WHERE version_id = %s", (version_id,), role=user.role)
    db.execute("DELETE FROM organisation_node WHERE version_id = %s", (version_id,), role=user.role)
    resume = organigramme_builder.construire(version_id, user.role)
    _log(version_id, user.id, "construction_reelle", "version", version_id, None, resume)
    return {"ok": True, "resume": resume, **oc.contenu(version_id, user.role)}


@router.delete("/admin/organigramme/versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> None:
    version = _version_ou_404(version_id, user.role)
    if version.get("statut") == "publie":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La version publiée ne peut pas être supprimée. Publiez-en une autre d'abord.")
    db.execute("DELETE FROM organisation_version WHERE id = %s", (version_id,), role=user.role)
    _log(None, user.id, "suppression_version", "version", version_id, {"libelle": version.get("libelle")}, None)


# --- Node and link edition --------------------------------------------------

@router.post("/admin/organigramme/versions/{version_id}/nodes", status_code=status.HTTP_201_CREATED)
def add_node(version_id: str, payload: NodeIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    if payload.statut not in oc.STATUTS_NOEUD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut de noeud invalide")
    row = db.execute(
        "INSERT INTO organisation_node (version_id, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, statut, pos_x, pos_y, couleur, afficher_photo, largeur, hauteur, ordre) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 500) RETURNING id",
        (version_id, payload.type_noeud, payload.nom, payload.sous_titre, payload.membre_id, payload.fonction_cle,
         payload.categorie, payload.unite_type, payload.unite_id, payload.statut, payload.pos_x, payload.pos_y,
         payload.couleur, payload.afficher_photo, payload.largeur, payload.hauteur),
        role=user.role,
    )
    nid = str(row["id"])
    _log(version_id, user.id, "ajout_noeud", "noeud", nid, None, {"nom": payload.nom, "type": payload.type_noeud})
    return {"id": nid}


@router.post("/admin/organigramme/nodes/{node_id}/affecter")
def affecter_membre(node_id: str, payload: AffectationIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    """Assign a member to a node and, optionally, mirror it onto the member profile.

    When ``appliquer_au_profil`` is set and the node carries a function, the member
    receives that function (confirmed) via the shared functions manager, or the
    ``est_berger`` consecration flag for a title category, so the chart drives the
    real member data instead of only drawing it. Fully audited.
    """
    node = db.fetch_one(
        "SELECT n.id, n.version_id, n.fonction_cle, n.categorie, n.nom, v.statut AS v_statut "
        "FROM organisation_node n JOIN organisation_version v ON v.id = n.version_id WHERE n.id = %s",
        (node_id,), role=user.role,
    )
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="noeud inconnu")
    if node.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    membre = None
    if payload.membre_id:
        membre = db.fetch_one("SELECT id, prenoms, nom, nom_affiche, genre FROM membre WHERE id = %s", (payload.membre_id,), role=user.role)
        if not membre:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="membre inconnu")
    # Update the node: bind the member and refresh its displayed name/status.
    nom_membre = None
    if membre:
        nom_membre = membre.get("nom_affiche") or f"{membre.get('prenoms') or ''} {membre.get('nom') or ''}".strip() or None
    db.execute(
        "UPDATE organisation_node SET membre_id = %s, statut = %s WHERE id = %s",
        (payload.membre_id, "actif" if payload.membre_id else "vacant", node_id), role=user.role,
    )
    applique = False
    fonction_cle = str(node.get("fonction_cle") or "").strip().lower()
    if payload.membre_id and payload.appliquer_au_profil and fonction_cle:
        if node.get("categorie") == "titre" or fonction_cle in fonctions_membre.TITRES_INTERDITS:
            # A title is granted through the consecration flag, never as a function.
            db.execute("UPDATE membre SET est_berger = true WHERE id = %s", (payload.membre_id,), role=user.role)
        else:
            connue = db.fetch_one("SELECT cle FROM fonction_honorifique WHERE cle = %s AND actif = true", (fonction_cle,), role=user.role)
            if connue:
                db.execute("UPDATE membre_fonction SET principale = false WHERE membre_id = %s", (payload.membre_id,), role=user.role)
                existe = db.fetch_one("SELECT id FROM membre_fonction WHERE membre_id = %s AND lower(fonction_cle) = %s", (payload.membre_id, fonction_cle), role=user.role)
                if existe:
                    db.execute("UPDATE membre_fonction SET confirmee = true, actif = true, principale = true, maj_le = now() WHERE id = %s", (existe["id"],), role=user.role)
                else:
                    db.execute("INSERT INTO membre_fonction (membre_id, fonction_cle, confirmee, actif, principale, ordre) VALUES (%s, %s, true, true, true, 0)", (payload.membre_id, str(connue["cle"])), role=user.role)
                fonctions_membre.sync_principale(payload.membre_id, user.role)
        applique = True
    _log(str(node["version_id"]), user.id, "affectation_membre", "noeud", node_id,
         {"nom": node.get("nom")}, {"membre_id": payload.membre_id, "profil": applique})
    audit.log(user.id, user.role, "organigramme_affectation", "membre", payload.membre_id or node_id,
              {"noeud": node_id, "fonction_cle": fonction_cle, "applique_au_profil": applique})
    return {"ok": True, "membre_nom": nom_membre, "profil_applique": applique}


@router.patch("/admin/organigramme/nodes/{node_id}")
def update_node(node_id: str, payload: NodePatch, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    node = db.fetch_one("SELECT n.*, v.statut AS v_statut FROM organisation_node n JOIN organisation_version v ON v.id = n.version_id WHERE n.id = %s", (node_id,), role=user.role)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="noeud inconnu")
    if node.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("statut") and fields["statut"] not in oc.STATUTS_NOEUD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut de noeud invalide")
    if not fields:
        return {"ok": True}
    sets = ", ".join(f"{k} = %s" for k in fields)
    db.execute(f"UPDATE organisation_node SET {sets} WHERE id = %s", (*fields.values(), node_id), role=user.role)
    _log(str(node["version_id"]), user.id, "maj_noeud", "noeud", node_id, {k: node.get(k) for k in fields}, fields)
    return {"ok": True}


@router.delete("/admin/organigramme/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(node_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> None:
    node = db.fetch_one("SELECT n.version_id, n.nom, v.statut AS v_statut FROM organisation_node n JOIN organisation_version v ON v.id = n.version_id WHERE n.id = %s", (node_id,), role=user.role)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="noeud inconnu")
    if node.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    db.execute("DELETE FROM organisation_node WHERE id = %s", (node_id,), role=user.role)
    _log(str(node["version_id"]), user.id, "suppression_noeud", "noeud", node_id, {"nom": node.get("nom")}, None)


@router.post("/admin/organigramme/versions/{version_id}/links", status_code=status.HTTP_201_CREATED)
def add_link(version_id: str, payload: LinkIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    if payload.type_lien not in oc.TYPES_LIEN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type de lien invalide")
    if payload.source_id == payload.cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un lien ne peut pas relier un noeud à lui-même")
    for nid in (payload.source_id, payload.cible_id):
        if not db.fetch_one("SELECT id FROM organisation_node WHERE id = %s AND version_id = %s", (nid, version_id), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="noeud source ou cible hors de la version")
    # A hierarchical link must not close a cycle in the reporting tree.
    if payload.type_lien == oc.LIEN_HIERARCHIQUE and oc.cree_un_cycle(version_id, payload.source_id, payload.cible_id, user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce lien hiérarchique créerait une boucle dans l'organigramme.")
    row = db.execute(
        "INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (version_id, payload.source_id, payload.cible_id, payload.type_lien, payload.libelle), role=user.role,
    )
    lid = str(row["id"])
    _log(version_id, user.id, "ajout_lien", "lien", lid, None, {"type": payload.type_lien})
    return {"id": lid}


@router.patch("/admin/organigramme/links/{link_id}")
def update_link(link_id: str, payload: LinkPatch, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    """Move, re-type or re-label a link without deleting it. Reconnecting an endpoint
    (source/cible) is how a mis-placed link is corrected, e.g. moving the supervision
    of the Patriarchs from the mission shepherd to the moderator. A hierarchical result
    that would close a reporting loop is refused (409)."""
    link = db.fetch_one(
        "SELECT l.id, l.version_id, l.source_id, l.cible_id, l.type_lien, l.libelle, v.statut AS v_statut "
        "FROM organisation_link l JOIN organisation_version v ON v.id = l.version_id WHERE l.id = %s",
        (link_id,), role=user.role,
    )
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lien inconnu")
    if link.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    vid = str(link["version_id"])
    champs = payload.model_fields_set
    new_source = payload.source_id if "source_id" in champs else str(link["source_id"])
    new_cible = payload.cible_id if "cible_id" in champs else str(link["cible_id"])
    new_type = payload.type_lien if "type_lien" in champs else str(link["type_lien"])
    new_libelle = payload.libelle if "libelle" in champs else link.get("libelle")
    if new_type not in oc.TYPES_LIEN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type de lien invalide")
    if new_source == new_cible:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un lien ne peut pas relier un noeud à lui-même")
    for nid in (new_source, new_cible):
        if not db.fetch_one("SELECT id FROM organisation_node WHERE id = %s AND version_id = %s", (nid, vid), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="noeud source ou cible hors de la version")
    if new_type == oc.LIEN_HIERARCHIQUE and oc.cree_un_cycle_maj(vid, link_id, new_source, new_cible, user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce lien hiérarchique créerait une boucle dans l'organigramme.")
    avant = {"source_id": str(link["source_id"]), "cible_id": str(link["cible_id"]), "type_lien": link["type_lien"], "libelle": link.get("libelle")}
    db.execute(
        "UPDATE organisation_link SET source_id = %s, cible_id = %s, type_lien = %s, libelle = %s WHERE id = %s",
        (new_source, new_cible, new_type, new_libelle, link_id), role=user.role,
    )
    apres = {"source_id": new_source, "cible_id": new_cible, "type_lien": new_type, "libelle": new_libelle}
    _log(vid, user.id, "modification_lien", "lien", link_id, avant, apres)
    return {"id": link_id, **apres}


@router.delete("/admin/organigramme/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> None:
    link = db.fetch_one("SELECT l.version_id, v.statut AS v_statut FROM organisation_link l JOIN organisation_version v ON v.id = l.version_id WHERE l.id = %s", (link_id,), role=user.role)
    if not link:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="lien inconnu")
    if link.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    db.execute("DELETE FROM organisation_link WHERE id = %s", (link_id,), role=user.role)
    _log(str(link["version_id"]), user.id, "suppression_lien", "lien", link_id, None, None)


# --- Validation and publication --------------------------------------------



@router.post("/admin/organigramme/versions/{version_id}/valider")
def valider_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> dict[str, Any]:
    """Structural check before publishing: cycles, orphan hierarchical nodes,
    vacant apex roles. Returns issues; an empty ``bloquants`` means publishable."""
    _version_ou_404(version_id, user.role)
    contenu = oc.contenu(version_id, user.role)
    bloquants: list[str] = []
    avertissements: list[str] = []
    if oc.detecte_cycle(oc.aretes_hierarchiques(version_id, user.role)):
        bloquants.append("Une boucle hiérarchique existe dans l'organigramme.")
    if not contenu["noeuds"]:
        bloquants.append("L'organigramme est vide.")
    vacants = [n["nom"] for n in contenu["noeuds"] if n.get("statut") == "vacant"]
    if vacants:
        avertissements.append(f"{len(vacants)} poste(s) vacant(s) : {', '.join(vacants[:6])}{'...' if len(vacants) > 6 else ''}.")
    # Coherence over the real organisation data (units, apex, interims, attachments).
    coh = organigramme_coherence.rapport(user.role)
    bloquants.extend(f["message"] for f in coh["bloquants"])
    avertissements.extend(f["message"] for f in coh["avertissements"])
    return {"publiable": not bloquants, "bloquants": bloquants, "avertissements": avertissements,
            "coherence": coh, "noeuds": len(contenu["noeuds"]), "liens": len(contenu["liens"])}


@router.get("/admin/organigramme/coherence")
def rapport_coherence(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> dict[str, Any]:
    """Standalone coherence report over the real organisation data, independent of any
    draft version, so the direction can audit the structure at any time."""
    return organigramme_coherence.rapport(user.role)


@router.post("/admin/organigramme/versions/{version_id}/publier")
def publier_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    if oc.detecte_cycle(oc.aretes_hierarchiques(version_id, user.role)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Publication impossible : une boucle hiérarchique existe.")
    if not db.fetch_one("SELECT id FROM organisation_node WHERE version_id = %s AND actif = true LIMIT 1", (version_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Publication impossible : l'organigramme est vide.")
    # Archive the current published version, then publish this one (single published
    # version enforced by a partial unique index).
    db.execute("UPDATE organisation_version SET statut = 'archive' WHERE statut = 'publie'", (), role=user.role)
    db.execute("UPDATE organisation_version SET statut = 'publie', publie_le = now(), publie_par = %s WHERE id = %s", (user.id, version_id), role=user.role)
    _log(version_id, user.id, "publication", "version", version_id, None, {"libelle": version.get("libelle")})
    return {"ok": True, "publie": True}


@router.post("/admin/organigramme/versions/{version_id}/restaurer", status_code=status.HTTP_201_CREATED)
def restaurer_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    """Clone any version into a new editable draft (rollback path)."""
    source = _version_ou_404(version_id, user.role)
    row = db.execute("INSERT INTO organisation_version (libelle, statut, note, cree_par, source_version_id) VALUES (%s, 'brouillon', %s, %s, %s) RETURNING id",
                     (f"Reprise de {source.get('libelle')}", source.get("note"), user.id, version_id), role=user.role)
    vid = str(row["id"])
    mapping: dict[str, str] = {}
    for n in db.fetch_all("SELECT * FROM organisation_node WHERE version_id = %s AND actif = true", (version_id,), role=user.role):
        new = db.execute(
            "INSERT INTO organisation_node (version_id, cle, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, effectif, statut, pos_x, pos_y, ordre, meta) "
            "SELECT %s, cle, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, effectif, statut, pos_x, pos_y, ordre, meta FROM organisation_node WHERE id = %s RETURNING id",
            (vid, n["id"]), role=user.role)
        mapping[str(n["id"])] = str(new["id"])
    for le in db.fetch_all("SELECT * FROM organisation_link WHERE version_id = %s AND actif = true", (version_id,), role=user.role):
        s, c = mapping.get(str(le["source_id"])), mapping.get(str(le["cible_id"]))
        if s and c:
            db.execute("INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES (%s, %s, %s, %s, %s)", (vid, s, c, le["type_lien"], le.get("libelle")), role=user.role)
    _log(vid, user.id, "restauration", "version", vid, {"source": version_id}, {"noeuds": len(mapping)})
    return {"id": vid, "noeuds": len(mapping)}


# --- Member-facing reads ----------------------------------------------------

@router.get("/organigramme/publie")
def organigramme_publie(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """The single published organisation chart, for the consultation view."""
    pub = db.fetch_one("SELECT id, libelle, statut, note, cree_le, publie_le FROM organisation_version WHERE statut = 'publie'", (), role=user.role)
    if not pub:
        return {"version": None, "noeuds": [], "liens": []}
    return {"version": oc.version_dict(pub), **oc.contenu(str(pub["id"]), user.role)}


@router.get("/organigramme/statistiques")
def organigramme_statistiques(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> dict[str, Any]:
    """Live, database-derived counters for the organisation, never hand-entered.

    ``effectif_unique`` counts distinct active members holding a responsibility;
    ``affectations_actives`` counts the responsibilities themselves, so the gap
    measures the cumul. Structural placement is reported apart and ``anomalies``
    lists the real integrity gaps. Computation lives in ``organigramme_stats``."""
    return organigramme_stats.calculer(user.role)


@router.get("/membres/me/hierarchie")
def ma_hierarchie(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """The connected member's full, FUNCTION-BASED hierarchy: every active function
    (multi-role), a personalised upward chain N+1..N, titles and particular links kept
    apart from the functional chain, and the member's units (vacant posts kept as
    'Poste a pourvoir'). Computation lives in ``hierarchie_membre``."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lié à un membre")
    return hierarchie_membre.calculer(user.membre_id, user.role)
