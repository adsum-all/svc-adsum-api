# ruff: noqa: E501 - long SQL statements
"""Custom board templates for the collaboration app (global library).

A custom template (columns with a name, a colour and an optional WIP limit) is a
LIBRARY asset, not tied to any workspace: once created it is available to every
workspace and every collaborator, alongside the built-in catalogue. Creating,
editing or deleting one requires the dedicated ``collaboration.modeles`` permission
(granted to super_admin/admin, or to a member through a governance group). Reading
the merged catalogue only needs ``collaboration.superviser`` like the built-in one.
Visibility is a library scope: ``partage`` (everyone) or ``prive`` (author only).
Deleting a template never touches boards already created from it: the columns are
copied at board creation, never referenced afterwards.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import audit, db
from .fields import LineStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-modeles"])

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Roles allowed to moderate (edit/delete) a shared template they did not author.
_MODERATEURS = ("super_admin", "admin")


class PublicationIn(BaseModel):
    publie: bool


class ArchivageIn(BaseModel):
    archive: bool


class ModeleColonneIn(BaseModel):
    nom: LineStr
    couleur: str | None = None
    wip: int | None = Field(default=None, ge=1, le=999)

    @field_validator("couleur")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _HEX.match(v):
            raise ValueError("couleur invalide (format attendu #rrggbb)")
        return v


class ModelePersoIn(BaseModel):
    # Templates are global; espace_id, if provided, only records the workspace the
    # author happened to build it from (informational, never a scope constraint).
    espace_id: str | None = None
    nom: TitleStr
    description: TextStr = ""
    visibilite: str = Field(default="partage", pattern="^(partage|prive)$")
    colonnes: list[ModeleColonneIn] = Field(min_length=1, max_length=12)


class ModelePersoPatch(BaseModel):
    nom: TitleStr | None = None
    description: TextStr | None = None
    visibilite: str | None = Field(default=None, pattern="^(partage|prive)$")
    colonnes: list[ModeleColonneIn] | None = Field(default=None, min_length=1, max_length=12)


def _structure(colonnes: list[ModeleColonneIn]) -> str:
    return json.dumps([{"nom": c.nom, "couleur": c.couleur, "wip": c.wip} for c in colonnes])


def _out(r: dict[str, Any]) -> dict[str, Any]:
    cols = r.get("structure")
    if isinstance(cols, str):
        cols = json.loads(cols or "[]")
    return {
        "id": str(r["id"]), "libelle": r.get("nom"),
        "description": r.get("description") or "", "visibilite": r.get("visibilite"),
        "cree_par": str(r["cree_par"]) if r.get("cree_par") else None,
        "source": "personnalise",
        "colonnes": [{"nom": c.get("nom"), "couleur": c.get("couleur"), "wip": c.get("wip")} for c in (cols or [])],
    }


def modeles_perso_visibles(user: UserMe) -> list[dict[str, Any]]:
    """The custom templates a collaborator may reuse: every shared template, plus the
    caller's own private ones. Global (not workspace-scoped). Used by the merged catalogue."""
    rows = db.fetch_all(
        "SELECT id, nom, description, structure, visibilite, cree_par FROM collab_modele_perso "
        "WHERE supprime_le IS NULL AND (visibilite = 'partage' OR cree_par = %s) "
        "ORDER BY nom",
        (user.id,), role=user.role,
    )
    return [_out(r) for r in rows]


def structure_perso(modele_id: str, user: UserMe) -> list[dict[str, Any]] | None:
    """Columns of a custom template the caller may use (shared, or their own private one).
    Returns None when the id is not a usable custom template (so the caller falls back cleanly)."""
    try:
        uuid.UUID(modele_id)
    except ValueError:
        return None
    row = db.fetch_one(
        "SELECT structure FROM collab_modele_perso WHERE id = %s AND supprime_le IS NULL "
        "AND (visibilite = 'partage' OR cree_par = %s)",
        (modele_id, user.id), role=user.role,
    )
    if not row:
        return None
    cols = row["structure"]
    if isinstance(cols, str):
        cols = json.loads(cols or "[]")
    return [{"nom": c.get("nom"), "couleur": c.get("couleur"), "wip": c.get("wip")} for c in (cols or [])]


def _modele_ou_404(modele_id: str, user: UserMe) -> dict[str, Any]:
    # A private template is visible (thus manageable) only by its author; a shared one is
    # visible to all. Moderation of a shared template is further gated in the callers.
    row = db.fetch_one(
        "SELECT * FROM collab_modele_perso WHERE id = %s AND supprime_le IS NULL "
        "AND (visibilite = 'partage' OR cree_par = %s)",
        (modele_id, user.id), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="modèle introuvable")
    return row


def _garde_gestion(modele: dict[str, Any], user: UserMe) -> None:
    """Only the author may edit or delete their template; a shared template may also be
    moderated by a global admin. Never let another collaboration.modeles holder touch it."""
    if str(modele.get("cree_par")) == user.id:
        return
    if user.role in _MODERATEURS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="seul l'auteur peut gérer ce modèle")


@router.get("/modeles-perso")
def list_modeles_perso(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> list[dict[str, Any]]:
    return modeles_perso_visibles(user)


@router.post("/modeles-perso", status_code=status.HTTP_201_CREATED)
def create_modele_perso(
    payload: ModelePersoIn, user: Annotated[UserMe, Depends(require_permission("collaboration.template.create"))],
) -> dict[str, Any]:
    row = db.execute(
        "INSERT INTO collab_modele_perso (espace_id, nom, description, structure, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, %s) RETURNING *",
        (payload.espace_id, payload.nom, payload.description, _structure(payload.colonnes), payload.visibilite, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "collab_modele_perso_creation", "collab_modele_perso", str((row or {}).get("id")), {"nom": payload.nom, "visibilite": payload.visibilite})
    return _out(row or {})


@router.patch("/modeles-perso/{modele_id}")
def update_modele_perso(
    modele_id: str, payload: ModelePersoPatch, user: Annotated[UserMe, Depends(require_permission("collaboration.template.update"))],
) -> dict[str, Any]:
    modele = _modele_ou_404(modele_id, user)
    _garde_gestion(modele, user)
    champs = payload.model_dump(exclude_unset=True)
    sets: list[str] = []
    vals: list[Any] = []
    if "nom" in champs and payload.nom is not None:
        sets.append("nom = %s")
        vals.append(payload.nom)
    if "description" in champs and payload.description is not None:
        sets.append("description = %s")
        vals.append(payload.description)
    if "visibilite" in champs and payload.visibilite is not None:
        sets.append("visibilite = %s")
        vals.append(payload.visibilite)
    if "colonnes" in champs and payload.colonnes is not None:
        sets.append("structure = %s::jsonb")
        vals.append(_structure(payload.colonnes))
    if not sets:
        return _out(modele)
    vals.append(modele_id)
    row = db.execute(f"UPDATE collab_modele_perso SET {', '.join(sets)}, maj_le = now() WHERE id = %s RETURNING *", tuple(vals), role=user.role)
    audit.log(user.id, user.role, "collab_modele_perso_maj", "collab_modele_perso", modele_id, {"champs": list(champs.keys())})
    return _out(row or {})


@router.delete("/modeles-perso/{modele_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_modele_perso(
    modele_id: str, user: Annotated[UserMe, Depends(require_permission("collaboration.template.delete"))],
) -> None:
    modele = _modele_ou_404(modele_id, user)
    _garde_gestion(modele, user)
    # Soft-delete: boards already created from this template keep their (copied) columns.
    db.execute("UPDATE collab_modele_perso SET supprime_le = now() WHERE id = %s", (modele_id,), role=user.role)
    audit.log(user.id, user.role, "collab_modele_perso_suppression", "collab_modele_perso", modele_id, {"nom": modele.get("nom")})


@router.post("/modeles-perso/{modele_id}/publication")
def publier_modele(
    modele_id: str,
    payload: PublicationIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.template.publish"))],
) -> dict[str, Any]:
    """Offer a template to the teams, or take it back to draft.

    Publishing is deliberately a separate right from creating: preparing a template
    and standing behind it are two different acts, and an organisation usually wants
    them held by different people.
    """
    modele = _modele_ou_404(modele_id, user)
    if modele.get("statut") == "archive":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="un modèle archivé doit être réactivé avant d'être publié")
    cible = "publie" if payload.publie else "brouillon"
    row = db.execute(
        "UPDATE collab_modele_perso SET statut = %s, publie_le = CASE WHEN %s THEN now() ELSE publie_le END, "
        "publie_par = CASE WHEN %s THEN %s ELSE publie_par END, maj_le = now() WHERE id = %s RETURNING *",
        (cible, payload.publie, payload.publie, user.id, modele_id), role=user.role,
    )
    audit.log(user.id, user.role, "collab_modele_publication", "collab_modele_perso", modele_id,
              {"statut": cible, "nom": modele.get("nom")})
    return _out(row or {})


@router.post("/modeles-perso/{modele_id}/archivage")
def archiver_modele(
    modele_id: str,
    payload: ArchivageIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.template.archive"))],
) -> dict[str, Any]:
    """Take a template out of the catalogue without destroying it.

    Boards already created from it keep working: their columns were copied at
    creation and never referenced afterwards. Archiving is therefore reversible and
    safe, which is why it is a lighter right than deleting.
    """
    modele = _modele_ou_404(modele_id, user)
    cible = "archive" if payload.archive else "brouillon"
    row = db.execute(
        "UPDATE collab_modele_perso SET statut = %s, archive_le = CASE WHEN %s THEN now() ELSE NULL END, "
        "archive_par = CASE WHEN %s THEN %s ELSE NULL END, maj_le = now() WHERE id = %s RETURNING *",
        (cible, payload.archive, payload.archive, user.id, modele_id), role=user.role,
    )
    audit.log(user.id, user.role, "collab_modele_archivage", "collab_modele_perso", modele_id,
              {"statut": cible, "nom": modele.get("nom")})
    return _out(row or {})
