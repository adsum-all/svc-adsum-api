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

from . import db, organigramme_builder
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["organigramme"])

_LIEN_HIERARCHIQUE = "hierarchique"
_TYPES_LIEN = {"hierarchique", "coordination", "supervision", "suivi_transversal", "responsabilite_tribu", "assistance"}
_STATUTS_NOEUD = {"actif", "vacant", "attente", "archive"}


# --- Serialisation ----------------------------------------------------------

def _node_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "cle": r.get("cle"), "type_noeud": r.get("type_noeud"),
        "nom": r.get("nom"), "sous_titre": r.get("sous_titre"),
        "membre_id": str(r["membre_id"]) if r.get("membre_id") else None,
        "fonction_cle": r.get("fonction_cle"), "categorie": r.get("categorie"),
        "unite_type": r.get("unite_type"), "unite_id": str(r["unite_id"]) if r.get("unite_id") else None,
        "effectif": r.get("effectif"), "statut": r.get("statut"),
        "pos_x": r.get("pos_x"), "pos_y": r.get("pos_y"), "ordre": r.get("ordre"),
    }


def _link_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "source_id": str(r["source_id"]), "cible_id": str(r["cible_id"]),
        "type_lien": r.get("type_lien"), "libelle": r.get("libelle"),
    }


def _version_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "libelle": r.get("libelle"), "statut": r.get("statut"),
        "note": r.get("note"), "cree_le": r.get("cree_le"), "publie_le": r.get("publie_le"),
    }


def _contenu(version_id: str, role: str | None) -> dict[str, Any]:
    nodes = db.fetch_all(
        "SELECT id, cle, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, "
        "effectif, statut, pos_x, pos_y, ordre FROM organisation_node WHERE version_id = %s AND actif = true ORDER BY ordre",
        (version_id,), role=role,
    )
    links = db.fetch_all(
        "SELECT id, source_id, cible_id, type_lien, libelle FROM organisation_link WHERE version_id = %s AND actif = true",
        (version_id,), role=role,
    )
    return {"noeuds": [_node_dict(n) for n in nodes], "liens": [_link_dict(le) for le in links]}


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
    type_noeud: str = Field(default="structure", pattern="^(personne|structure|groupe)$")
    sous_titre: str | None = Field(default=None, max_length=200)
    membre_id: str | None = None
    fonction_cle: str | None = Field(default=None, max_length=40)
    categorie: str | None = Field(default=None, max_length=40)
    unite_type: str | None = Field(default=None, max_length=40)
    unite_id: str | None = None
    statut: str = Field(default="actif")
    pos_x: float | None = None
    pos_y: float | None = None


class NodePatch(BaseModel):
    nom: str | None = Field(default=None, max_length=160)
    sous_titre: str | None = Field(default=None, max_length=200)
    membre_id: str | None = None
    statut: str | None = None
    pos_x: float | None = None
    pos_y: float | None = None


class LinkIn(BaseModel):
    source_id: str
    cible_id: str
    type_lien: str = Field(default="hierarchique")
    libelle: str | None = Field(default=None, max_length=120)


# --- Version lifecycle ------------------------------------------------------

@router.get("/admin/organigramme/versions")
def list_versions(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT id, libelle, statut, note, cree_le, publie_le FROM organisation_version ORDER BY cree_le DESC", (), role=user.role)
    return [_version_dict(r) for r in rows]


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
    return {"version": _version_dict(version), **_contenu(version_id, user.role)}


@router.post("/admin/organigramme/versions/{version_id}/construire")
def construire_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    """(Re)build a draft from the real data: clears its nodes/links then rebuilds."""
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    db.execute("DELETE FROM organisation_link WHERE version_id = %s", (version_id,), role=user.role)
    db.execute("DELETE FROM organisation_node WHERE version_id = %s", (version_id,), role=user.role)
    resume = organigramme_builder.construire(version_id, user.role)
    _log(version_id, user.id, "construction_reelle", "version", version_id, None, resume)
    return {"ok": True, "resume": resume, **_contenu(version_id, user.role)}


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
    if payload.statut not in _STATUTS_NOEUD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut de noeud invalide")
    row = db.execute(
        "INSERT INTO organisation_node (version_id, type_noeud, nom, sous_titre, membre_id, fonction_cle, categorie, unite_type, unite_id, statut, pos_x, pos_y, ordre) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 500) RETURNING id",
        (version_id, payload.type_noeud, payload.nom, payload.sous_titre, payload.membre_id, payload.fonction_cle,
         payload.categorie, payload.unite_type, payload.unite_id, payload.statut, payload.pos_x, payload.pos_y),
        role=user.role,
    )
    nid = str(row["id"])
    _log(version_id, user.id, "ajout_noeud", "noeud", nid, None, {"nom": payload.nom})
    return {"id": nid}


@router.patch("/admin/organigramme/nodes/{node_id}")
def update_node(node_id: str, payload: NodePatch, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    node = db.fetch_one("SELECT n.*, v.statut AS v_statut FROM organisation_node n JOIN organisation_version v ON v.id = n.version_id WHERE n.id = %s", (node_id,), role=user.role)
    if not node:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="noeud inconnu")
    if node.get("v_statut") != "brouillon":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une version en brouillon peut être modifiée.")
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("statut") and fields["statut"] not in _STATUTS_NOEUD:
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
    if payload.type_lien not in _TYPES_LIEN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type de lien invalide")
    if payload.source_id == payload.cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="un lien ne peut pas relier un noeud à lui-même")
    for nid in (payload.source_id, payload.cible_id):
        if not db.fetch_one("SELECT id FROM organisation_node WHERE id = %s AND version_id = %s", (nid, version_id), role=user.role):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="noeud source ou cible hors de la version")
    # A hierarchical link must not close a cycle in the reporting tree.
    if payload.type_lien == _LIEN_HIERARCHIQUE and _cree_un_cycle(version_id, payload.source_id, payload.cible_id, user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce lien hiérarchique créerait une boucle dans l'organigramme.")
    row = db.execute(
        "INSERT INTO organisation_link (version_id, source_id, cible_id, type_lien, libelle) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (version_id, payload.source_id, payload.cible_id, payload.type_lien, payload.libelle), role=user.role,
    )
    lid = str(row["id"])
    _log(version_id, user.id, "ajout_lien", "lien", lid, None, {"type": payload.type_lien})
    return {"id": lid}


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

def _aretes_hierarchiques(version_id: str, role: str | None, extra: tuple[str, str] | None = None) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for le in db.fetch_all("SELECT source_id, cible_id FROM organisation_link WHERE version_id = %s AND actif = true AND type_lien = %s", (version_id, _LIEN_HIERARCHIQUE), role=role):
        adj.setdefault(str(le["source_id"]), []).append(str(le["cible_id"]))
    if extra:
        adj.setdefault(extra[0], []).append(extra[1])
    return adj


def _detecte_cycle(adj: dict[str, list[str]]) -> bool:
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


def _cree_un_cycle(version_id: str, source_id: str, cible_id: str, role: str | None) -> bool:
    return _detecte_cycle(_aretes_hierarchiques(version_id, role, extra=(source_id, cible_id)))


@router.post("/admin/organigramme/versions/{version_id}/valider")
def valider_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> dict[str, Any]:
    """Structural check before publishing: cycles, orphan hierarchical nodes,
    vacant apex roles. Returns issues; an empty ``bloquants`` means publishable."""
    _version_ou_404(version_id, user.role)
    contenu = _contenu(version_id, user.role)
    bloquants: list[str] = []
    avertissements: list[str] = []
    if _detecte_cycle(_aretes_hierarchiques(version_id, user.role)):
        bloquants.append("Une boucle hiérarchique existe dans l'organigramme.")
    if not contenu["noeuds"]:
        bloquants.append("L'organigramme est vide.")
    vacants = [n["nom"] for n in contenu["noeuds"] if n.get("statut") == "vacant"]
    if vacants:
        avertissements.append(f"{len(vacants)} poste(s) vacant(s) : {', '.join(vacants[:6])}{'...' if len(vacants) > 6 else ''}.")
    return {"publiable": not bloquants, "bloquants": bloquants, "avertissements": avertissements,
            "noeuds": len(contenu["noeuds"]), "liens": len(contenu["liens"])}


@router.post("/admin/organigramme/versions/{version_id}/publier")
def publier_version(version_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))]) -> dict[str, Any]:
    version = _version_ou_404(version_id, user.role)
    _brouillon_ou_409(version)
    if _detecte_cycle(_aretes_hierarchiques(version_id, user.role)):
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
    return {"version": _version_dict(pub), **_contenu(str(pub["id"]), user.role)}


@router.get("/membres/me/hierarchie")
def ma_hierarchie(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """The connected member's own upward hierarchy, derived from the real data:
    their units and responsibles, the functional apex chain, and their tribe with
    its patriarch. Never exposes member lists the caller is not allowed to see."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lié à un membre")
    m = db.fetch_one(
        "SELECT id, prenoms, nom, nom_affiche, commission_id, coordination_id, intendance_id, tribu_id FROM membre WHERE id = %s",
        (user.membre_id,), role=user.role,
    ) or {}

    def unite(table: str, uid: object, fonction: str) -> dict[str, Any] | None:
        if not uid:
            return None
        row = db.fetch_one(f"SELECT id, nom, responsable_id FROM {table} WHERE id = %s", (uid,), role=user.role)
        if not row:
            return None
        resp = db.fetch_one("SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s", (row.get("responsable_id"),), role=user.role) if row.get("responsable_id") else None
        nom_resp = None
        if resp:
            nom_resp = str(resp.get("nom_affiche") or f"{resp.get('prenoms') or ''} {resp.get('nom') or ''}".strip()) or None
        return {"nom": row.get("nom"), "responsable": nom_resp, "fonction": fonction}

    tribu = None
    if m.get("tribu_id"):
        tr = db.fetch_one("SELECT nom, patriarche, patriarche_membre_id FROM tribu WHERE id = %s", (m["tribu_id"],), role=user.role)
        if tr:
            patr = db.fetch_one("SELECT prenoms, nom, nom_affiche FROM membre WHERE id = %s", (tr.get("patriarche_membre_id"),), role=user.role) if tr.get("patriarche_membre_id") else None
            nom_patr = str(patr.get("nom_affiche") or f"{patr.get('prenoms') or ''} {patr.get('nom') or ''}".strip()) if patr else (tr.get("patriarche") or None)
            tribu = {"nom": tr.get("nom"), "patriarche": nom_patr}

    chaine = []
    for fcle in ("intendant_general", "controleur_general", "berger_missions", "moderateur", "fondateur"):
        h = organigramme_builder._titulaire_fonction(fcle, user.role)
        libelle = fcle.replace("_", " ").title()
        chaine.append({"fonction": libelle, "titulaire": organigramme_builder._nom_membre(h)})

    return {
        "commission": unite("commission", m.get("commission_id"), "Responsable"),
        "coordination": unite("coordination", m.get("coordination_id"), "Coordinateur"),
        "intendance": unite("intendance", m.get("intendance_id"), "Intendant"),
        "tribu": tribu,
        "chaine_fonctionnelle": chaine,
    }
