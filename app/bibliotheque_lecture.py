"""Let a member read the institution's documents, and let the governance see who signed what.

A member could read a consent text at the moment they signed it and never again: the
text was hidden as soon as a new version replaced it, and nothing let them take a copy
away. Meanwhile the proofs of signature were visible only inside one registration file,
so "who has signed the charter" had no answer short of opening every file one by one.

Two readings, one module: what a member may read, and what the governance may verify.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import db, storage
from .permissions_rbac import require_permission
from .schemas import UserMe

# Served under /membres/me/bibliotheque, not /membres/me/documents: that path
# already lists the member's own file pieces (membres.py), and taking it would
# have replaced their identity documents with the institution's library.
router = APIRouter(prefix="/api/v1", tags=["documents"])

# What each audience is allowed to see. A member reads what is addressed to members
# and what is public; the governance reading goes through the back-office routes.
_VISIBLE_MEMBRE = ("public", "membres")


@router.get("/membres/me/bibliotheque")
def documents_du_membre(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
    categorie: str | None = Query(default=None),
) -> dict[str, Any]:
    """The published documents this member may read, with their current version."""
    conditions = ["d.statut = 'publie'", "d.visibilite = ANY(%s)"]
    params: list[Any] = [list(_VISIBLE_MEMBRE)]
    if categorie:
        conditions.append("d.categorie = %s")
        params.append(categorie)
    where = "WHERE " + " AND ".join(conditions)
    lignes = db.fetch_all(
        "SELECT d.id, d.cle, d.categorie, d.titre, d.titre_en, d.description, "
        "v.id AS version_id, v.version, v.contenu, v.contenu_en, v.nom_fichier, v.mime, "
        "v.taille, v.publie_le "
        "FROM document_institutionnel d "
        "JOIN LATERAL ("
        "  SELECT * FROM document_institutionnel_version vv "
        "   WHERE vv.document_id = d.id AND vv.publie_le IS NOT NULL "
        "   ORDER BY vv.version DESC LIMIT 1"
        ") v ON true "
        f"{where} ORDER BY d.ordre, d.titre",
        tuple(params), role=user.role,
    )
    return {
        "items": [
            {
                "id": str(r["id"]),
                "cle": r.get("cle"),
                "categorie": r.get("categorie"),
                "titre": r.get("titre"),
                "titre_en": r.get("titre_en"),
                "description": r.get("description"),
                "version": r.get("version"),
                "version_id": str(r["version_id"]),
                "contenu": r.get("contenu"),
                "contenu_en": r.get("contenu_en"),
                # Presence of a file, not its path: the download goes through a
                # short-lived signed link so the bucket is never addressable directly.
                "fichier": ({"nom": r.get("nom_fichier"), "mime": r.get("mime"), "taille": r.get("taille")}
                            if r.get("nom_fichier") else None),
                "publie_le": r.get("publie_le"),
            }
            for r in lignes
        ],
        "total": len(lignes),
    }


@router.get("/membres/me/bibliotheque/{version_id}/fichier")
def telecharger(
    version_id: str,
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, Any]:
    """A short-lived link to the file attached to a published version.

    Checked against the reader's audience rather than trusting the identifier: a
    version id is guessable enough that serving it on sight would let a member pull a
    document the governance keeps to itself.
    """
    r = db.fetch_one(
        "SELECT v.bucket, v.chemin, v.nom_fichier, d.visibilite, d.statut "
        "FROM document_institutionnel_version v "
        "JOIN document_institutionnel d ON d.id = v.document_id "
        "WHERE v.id = %s AND v.publie_le IS NOT NULL",
        (version_id,), role=user.role,
    )
    if not r or str(r.get("statut")) != "publie" or str(r.get("visibilite")) not in _VISIBLE_MEMBRE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document indisponible")
    if not r.get("chemin"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="aucun fichier joint à cette version")
    try:
        url = storage.signed_download_url(str(r.get("bucket") or ""), str(r["chemin"]))
    except storage.StorageError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="stockage indisponible, réessayez dans un instant") from exc
    return {"url": url, "nom": r.get("nom_fichier")}


@router.get("/admin/consentements/tracabilite")
def tracabilite(
    user: Annotated[UserMe, Depends(require_permission("consentements.consulter"))],
    cle: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Who signed which consent text, in which version, and on what proof.

    The proofs existed from the start and were readable only from inside one
    registration file, one member at a time. Asking "who has signed the charter, and
    who has not" therefore had no answer, which is exactly the question a consent
    ledger exists to answer.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if cle:
        conditions.append("e.type = %s")
        params.append(cle)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM engagement e {where}", tuple(params), role=user.role,
    ) or {}).get("n") or 0)
    lignes = db.fetch_all(
        "SELECT e.id, e.type, e.version, e.signe_le, e.canal, e.code_verifie, "
        "e.hash_preuve, e.document_consentement_id, "
        "m.prenoms, m.nom, m.email, m.matricule, d.titre AS document_titre "
        "FROM engagement e "
        "JOIN membre m ON m.id = e.membre_id "
        "LEFT JOIN document_consentement d ON d.id = e.document_consentement_id "
        f"{where} ORDER BY e.signe_le DESC NULLS LAST LIMIT %s OFFSET %s",
        (*params, taille, (page - 1) * taille), role=user.role,
    )
    # Per text: how many signed it, so a gap is visible without counting by hand.
    parTexte = db.fetch_all(
        "SELECT d.cle, d.titre, d.version, d.bloquant, "
        "(SELECT count(DISTINCT e.membre_id) FROM engagement e WHERE e.type = d.cle) AS signataires "
        "FROM document_consentement d WHERE d.actif = true ORDER BY d.ordre, d.titre",
        (), role=user.role,
    )
    concernes = int((db.fetch_one(
        "SELECT COUNT(*) AS n FROM membre WHERE statut = 'actif' AND statut_inscription = 'approuve'",
        (), role=user.role,
    ) or {}).get("n") or 0)
    return {
        "items": [
            {
                "id": str(r["id"]),
                "membre": f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or r.get("email"),
                "email": r.get("email"),
                "matricule": r.get("matricule"),
                "texte": r.get("document_titre") or r.get("type"),
                "cle": r.get("type"),
                "version": r.get("version"),
                "signe_le": r.get("signe_le"),
                "canal": r.get("canal"),
                "code_verifie": bool(r.get("code_verifie")),
                # A prefix only: the full fingerprint proves nothing more on screen
                # and a full hash invites being pasted where it should not go.
                "preuve": (str(r["hash_preuve"])[:16] if r.get("hash_preuve") else None),
                "rattache_au_texte": r.get("document_consentement_id") is not None,
            }
            for r in lignes
        ],
        "total": total, "page": page, "taille": taille,
        "pages": max(1, (total + taille - 1) // taille),
        "membres_concernes": concernes,
        "par_texte": [
            {
                "cle": t.get("cle"), "titre": t.get("titre"), "version": t.get("version"),
                "bloquant": bool(t.get("bloquant")), "signataires": int(t.get("signataires") or 0),
                "manquants": max(0, concernes - int(t.get("signataires") or 0)),
            }
            for t in parTexte
        ],
    }
