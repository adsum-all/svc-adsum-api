"""The institution's own documents, written once and readable for ever after.

Consent texts were the only institutional writing the platform held, under a single
``actif`` flag: publishing a new version silently hid the previous one, and a member
who had signed version 2 could no longer read what version 2 said. For a text somebody
signs, that is the point of keeping it.

Here a document has a life: a draft nobody sees, a published version everybody
concerned does, an archive that stays readable. Publishing never overwrites; it opens
the next version and stamps a fingerprint of exactly what went out.

Nothing is generated as PDF server side: no such library is installed, and adding one
to render text the browser already renders would be a dependency bought for nothing.
A file that arrives as a PDF is stored and served as one, through a short-lived signed
link, and a text written here is printed by the reader.
"""
# ruff: noqa: E501
from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import audit, db
from .config import settings
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/bibliotheque", tags=["documents"])

CATEGORIES = ("statuts", "reglement", "charte", "procedure", "note", "formulaire")
VISIBILITES = ("public", "membres", "gouvernance")


def _empreinte(contenu: str | None, contenu_en: str | None, chemin: str | None) -> str:
    """Fingerprint of what a version actually publishes.

    Covers both languages and the attached file's path: a version whose French text
    is unchanged but whose English translation was corrected is a different version,
    and saying otherwise would let a signature point at wording nobody approved.
    """
    brut = f"{contenu or ''}\x1f{contenu_en or ''}\x1f{chemin or ''}"
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def _document(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "cle": r.get("cle"),
        "categorie": r.get("categorie"),
        "titre": r.get("titre"),
        "titre_en": r.get("titre_en"),
        "description": r.get("description"),
        "visibilite": r.get("visibilite"),
        "statut": r.get("statut"),
        "ordre": r.get("ordre"),
        "version_publiee": r.get("version_publiee"),
        "versions": r.get("nb_versions"),
        "maj_le": r.get("maj_le") or r.get("cree_le"),
    }


@router.get("")
def lister(
    user: Annotated[UserMe, Depends(require_permission("documents.bibliotheque"))],
    statut: str | None = Query(default=None),
    categorie: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """The library, with each document's published version and how many it has."""
    conditions: list[str] = []
    params: list[Any] = []
    if statut:
        conditions.append("d.statut = %s")
        params.append(statut)
    if categorie:
        conditions.append("d.categorie = %s")
        params.append(categorie)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM document_institutionnel d {where}", tuple(params), role=user.role,
    ) or {}).get("n") or 0)
    lignes = db.fetch_all(
        "SELECT d.*, "
        "(SELECT max(v.version) FROM document_institutionnel_version v "
        "  WHERE v.document_id = d.id AND v.publie_le IS NOT NULL) AS version_publiee, "
        "(SELECT count(*) FROM document_institutionnel_version v WHERE v.document_id = d.id) AS nb_versions "
        f"FROM document_institutionnel d {where} ORDER BY d.ordre, d.titre LIMIT %s OFFSET %s",
        (*params, taille, (page - 1) * taille), role=user.role,
    )
    par_statut = db.fetch_all(
        "SELECT statut, COUNT(*) AS n FROM document_institutionnel GROUP BY statut", (), role=user.role,
    )
    return {
        "items": [_document(r) for r in lignes],
        "total": total, "page": page, "taille": taille,
        "pages": max(1, (total + taille - 1) // taille),
        "resume": {str(r["statut"]): int(r["n"]) for r in par_statut},
        "categories": list(CATEGORIES),
        "visibilites": list(VISIBILITES),
    }


@router.get("/{document_id}")
def fiche(
    document_id: str,
    user: Annotated[UserMe, Depends(require_permission("documents.bibliotheque"))],
) -> dict[str, Any]:
    """One document with every version it has ever had, newest first."""
    r = db.fetch_one(
        "SELECT d.*, "
        "(SELECT max(v.version) FROM document_institutionnel_version v "
        "  WHERE v.document_id = d.id AND v.publie_le IS NOT NULL) AS version_publiee, "
        "(SELECT count(*) FROM document_institutionnel_version v WHERE v.document_id = d.id) AS nb_versions "
        "FROM document_institutionnel d WHERE d.id = %s",
        (document_id,), role=user.role,
    )
    if not r:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document inconnu")
    versions = db.fetch_all(
        "SELECT v.id, v.version, v.contenu, v.contenu_en, v.nom_fichier, v.mime, v.taille, "
        "v.empreinte, v.notes, v.publie_le, v.cree_le, u.email AS publie_par_email "
        "FROM document_institutionnel_version v "
        "LEFT JOIN utilisateur u ON u.id = v.publie_par "
        "WHERE v.document_id = %s ORDER BY v.version DESC",
        (document_id,), role=user.role,
    )
    return {
        **_document(r),
        "versions_detail": [
            {
                "id": str(v["id"]),
                "version": v["version"],
                "contenu": v.get("contenu"),
                "contenu_en": v.get("contenu_en"),
                "fichier": ({"nom": v.get("nom_fichier"), "mime": v.get("mime"), "taille": v.get("taille")}
                            if v.get("nom_fichier") else None),
                "empreinte": (str(v["empreinte"])[:16] if v.get("empreinte") else None),
                "notes": v.get("notes"),
                "publiee": v.get("publie_le") is not None,
                "publie_le": v.get("publie_le"),
                "publie_par": v.get("publie_par_email"),
                "cree_le": v.get("cree_le"),
            }
            for v in versions
        ],
    }


class DocumentIn(BaseModel):
    cle: str = Field(min_length=2, max_length=80)
    titre: str = Field(min_length=2, max_length=200)
    titre_en: str | None = Field(default=None, max_length=200)
    categorie: str = Field(default="reglement")
    description: str | None = Field(default=None, max_length=1000)
    visibilite: str = Field(default="membres")
    ordre: int = Field(default=100, ge=0, le=9999)


@router.post("", status_code=status.HTTP_201_CREATED)
def creer(
    payload: DocumentIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("documents.bibliotheque"))],
) -> dict[str, Any]:
    """Create a document. It starts as a draft, visible to nobody but its authors."""
    if payload.categorie not in CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"catégorie inconnue, attendu : {', '.join(CATEGORIES)}")
    if payload.visibilite not in VISIBILITES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"visibilité inconnue, attendu : {', '.join(VISIBILITES)}")
    cle = payload.cle.strip().lower().replace(" ", "-")
    if db.fetch_one("SELECT id FROM document_institutionnel WHERE cle = %s", (cle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette clé est déjà utilisée")
    ligne = db.execute(
        "INSERT INTO document_institutionnel (cle, categorie, titre, titre_en, description, "
        "visibilite, ordre, cree_par) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (cle, payload.categorie, payload.titre.strip(), payload.titre_en, payload.description,
         payload.visibilite, payload.ordre, user.id), role=user.role,
    )
    did = str(ligne["id"]) if ligne else None
    audit.log(user.id, user.role, "document_institutionnel_cree", "document_institutionnel", did,
              {"cle": cle, "categorie": payload.categorie})
    return {"id": did, "cle": cle, "statut": "brouillon"}


class VersionIn(BaseModel):
    contenu: str | None = None
    contenu_en: str | None = None
    nom_fichier: str | None = Field(default=None, max_length=255)
    bucket: str | None = Field(default=None, max_length=80)
    chemin: str | None = Field(default=None, max_length=500)
    mime: str | None = Field(default=None, max_length=120)
    taille: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)
    publier: bool = False


def _valider_piece(payload: VersionIn, document_id: str) -> tuple[str | None, str | None]:
    """Confine an attached file to this document's own folder in the documents bucket.

    Both fields used to be free text, written down as given and later handed to the
    signing helper. Whoever could write a version could therefore point one at any
    object in any bucket, publish it, and read back a signed link to it: the members'
    identity pieces live in those buckets. The path is rebuilt here from the document
    identifier and a sanitised file name, so a version can only ever name a file that
    belongs to it.
    """
    if not payload.chemin and not payload.nom_fichier:
        return None, None
    if not payload.nom_fichier:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="une pièce jointe doit porter un nom de fichier")
    # Keep the leaf only, and only characters that cannot walk out of the folder.
    brut = payload.nom_fichier.replace("\\", "/").rsplit("/", 1)[-1]
    net = "".join(c for c in brut if c.isalnum() or c in "-_. ").strip()
    if not net or net.startswith("."):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="nom de fichier invalide")
    return settings.storage_bucket_documents, f"bibliotheque/{document_id}/{net}"


@router.post("/{document_id}/versions", status_code=status.HTTP_201_CREATED)
def nouvelle_version(
    document_id: str,
    payload: VersionIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("documents.bibliotheque"))],
) -> dict[str, Any]:
    """Open the next version of a document, optionally publishing it at once.

    A version is never edited in place once published: what was signed has to remain
    readable exactly as it was, so a correction is the next version, not a rewrite of
    the last one.
    """
    if not payload.contenu and not payload.chemin:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="une version porte un texte, un fichier, ou les deux")
    bucket, chemin = _valider_piece(payload, document_id)
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, cle, statut FROM document_institutionnel WHERE id = %s FOR UPDATE", (document_id,))
        doc = cur.fetchone()
        if not doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document inconnu")
        cur.execute(
            "SELECT COALESCE(max(version), 0) + 1 AS suivante FROM document_institutionnel_version "
            "WHERE document_id = %s", (document_id,),
        )
        version = int((cur.fetchone() or {}).get("suivante") or 1)
        cur.execute(
            "INSERT INTO document_institutionnel_version (document_id, version, contenu, contenu_en, "
            "bucket, chemin, nom_fichier, mime, taille, empreinte, notes, publie_le, publie_par) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s THEN now() ELSE NULL END, CASE WHEN %s THEN %s::uuid ELSE NULL END) RETURNING id",
            (document_id, version, payload.contenu, payload.contenu_en, bucket, chemin,
             payload.nom_fichier, payload.mime, payload.taille,
             _empreinte(payload.contenu, payload.contenu_en, chemin), payload.notes,
             payload.publier, payload.publier, user.id),
        )
        vid = str((cur.fetchone() or {}).get("id"))
        if payload.publier:
            cur.execute(
                "UPDATE document_institutionnel SET statut = 'publie', maj_le = now(), maj_par = %s WHERE id = %s",
                (user.id, document_id),
            )
    audit.log(user.id, user.role, "document_institutionnel_version", "document_institutionnel", document_id,
              {"version": version, "publiee": payload.publier})
    return {"id": vid, "version": version, "publiee": payload.publier}


@router.post("/{document_id}/versions/{version_id}/publier")
def publier(
    document_id: str,
    version_id: str,
    user: Annotated[UserMe, Depends(require_permission_ecriture("documents.bibliotheque"))],
) -> dict[str, Any]:
    """Publish a version that was kept as a draft."""
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE document_institutionnel_version SET publie_le = now(), publie_par = %s "
            "WHERE id = %s AND document_id = %s AND publie_le IS NULL RETURNING version",
            (user.id, version_id, document_id),
        )
        ligne = cur.fetchone()
        if not ligne:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="version introuvable ou déjà publiée")
        cur.execute(
            "UPDATE document_institutionnel SET statut = 'publie', maj_le = now(), maj_par = %s WHERE id = %s",
            (user.id, document_id),
        )
    audit.log(user.id, user.role, "document_institutionnel_publie", "document_institutionnel", document_id,
              {"version": ligne["version"]})
    return {"document_id": document_id, "version": ligne["version"], "publiee": True}


class StatutIn(BaseModel):
    statut: str


@router.patch("/{document_id}")
def changer_statut(
    document_id: str,
    payload: StatutIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("documents.bibliotheque"))],
) -> dict[str, Any]:
    """Move a document between draft, published and archived.

    Archiving hides it from the readers without deleting anything: a rule that no
    longer applies still has to be readable by whoever followed it at the time.
    """
    if payload.statut not in ("brouillon", "publie", "archive"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut inconnu")
    ligne = db.execute(
        "UPDATE document_institutionnel SET statut = %s, maj_le = now(), maj_par = %s "
        "WHERE id = %s RETURNING cle",
        (payload.statut, user.id, document_id), role=user.role,
    )
    if not ligne:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document inconnu")
    audit.log(user.id, user.role, "document_institutionnel_statut", "document_institutionnel", document_id,
              {"statut": payload.statut})
    return {"id": document_id, "statut": payload.statut}
