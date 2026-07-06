"""Governed tag catalogue and event tagging.

Tags are never free text: they belong to a fixed family and live in a catalogue
managed centrally. A responsable de perimetre cannot invent labels; they choose
from the catalogue and can request a new tag through a validation workflow. Tags
are a discovery/diffusion facet, not the security boundary (that is the scope).
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .auth import current_user
from .fields import ShortStr, TextStr, TitleStr
from .perimetre import PerimetreContext, require_perimetre
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["tags"])

_FAMILLES = ("diffusion", "territoire", "organisation", "type_activite", "transverse")


class TagIn(BaseModel):
    cle: ShortStr
    libelle: TitleStr
    famille: ShortStr
    description: TextStr | None = None


class TagDemandeIn(BaseModel):
    famille: ShortStr
    libelle: TitleStr
    justification: TextStr | None = None


class DecisionIn(BaseModel):
    statut: ShortStr  # accepte | refuse
    cle: ShortStr | None = None  # required when accepting: the catalogue key to create


@router.get("/tags")
def lister_tags(user: Annotated[UserMe, Depends(current_user)], famille: str | None = None) -> list[dict[str, object]]:
    """Active tags of the catalogue, optionally filtered by family (for selection)."""
    if famille:
        rows = db.fetch_all("SELECT id, cle, libelle, famille, description FROM tag WHERE actif = true AND famille = %s ORDER BY libelle ASC", (famille,), role=user.role)
    else:
        rows = db.fetch_all("SELECT id, cle, libelle, famille, description FROM tag WHERE actif = true ORDER BY famille ASC, libelle ASC", (), role=user.role)
    return [{"id": str(r["id"]), "cle": r["cle"], "libelle": r["libelle"], "famille": r["famille"], "description": r.get("description")} for r in rows]


@router.post("/admin/tags", status_code=status.HTTP_201_CREATED)
def creer_tag(payload: TagIn, user: Annotated[UserMe, Depends(require_permission("tags.administrer"))]) -> dict[str, str]:
    """Create a catalogue tag (central governance)."""
    if payload.famille not in _FAMILLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="famille invalide")
    created = db.execute(
        "INSERT INTO tag (cle, libelle, famille, description) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (cle) DO NOTHING RETURNING id",
        (payload.cle.strip().lower(), payload.libelle, payload.famille, payload.description),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cle deja utilisee")
    audit.log(user.id, user.role, "creation_tag", "tag", str(created["id"]), {"cle": payload.cle})
    return {"id": str(created["id"])}


@router.post("/pilotage/tags/demandes", status_code=status.HTTP_201_CREATED)
def demander_tag(payload: TagDemandeIn, ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, str]:
    """A responsable requests a new tag, submitted to central validation."""
    if payload.famille not in _FAMILLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="famille invalide")
    created = db.execute(
        "INSERT INTO tag_demande (proposant_membre_id, famille, libelle, justification) VALUES (%s, %s, %s, %s) RETURNING id",
        (ctx.user.membre_id, payload.famille, payload.libelle, payload.justification),
        role=ctx.user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="demande impossible")
    return {"id": str(created["id"])}


@router.get("/admin/tags/demandes")
def lister_demandes(user: Annotated[UserMe, Depends(require_permission("tags.administrer"))], statut: str | None = None) -> list[dict[str, object]]:
    """Pending or all tag requests, for central review."""
    if statut:
        rows = db.fetch_all("SELECT id, famille, libelle, justification, statut, cree_le FROM tag_demande WHERE statut = %s ORDER BY cree_le DESC LIMIT 200", (statut,), role=user.role)
    else:
        rows = db.fetch_all("SELECT id, famille, libelle, justification, statut, cree_le FROM tag_demande ORDER BY cree_le DESC LIMIT 200", (), role=user.role)
    return [{"id": str(r["id"]), "famille": r["famille"], "libelle": r["libelle"], "justification": r.get("justification"), "statut": r["statut"]} for r in rows]


@router.post("/admin/tags/demandes/{demande_id}/decision")
def decider_demande(demande_id: str, payload: DecisionIn, user: Annotated[UserMe, Depends(require_permission("tags.administrer"))]) -> dict[str, object]:
    """Accept (creating the catalogue tag) or refuse a tag request."""
    if payload.statut not in ("accepte", "refuse"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut invalide")
    dem = db.fetch_one("SELECT famille, libelle, statut FROM tag_demande WHERE id = %s", (demande_id,), role=user.role)
    if not dem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="demande introuvable")
    tag_id = None
    if payload.statut == "accepte":
        if not payload.cle:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cle requise pour accepter")
        created = db.execute(
            "INSERT INTO tag (cle, libelle, famille) VALUES (%s, %s, %s) ON CONFLICT (cle) DO NOTHING RETURNING id",
            (payload.cle.strip().lower(), dem["libelle"], dem["famille"]),
            role=user.role,
        )
        tag_id = str(created["id"]) if created else None
    db.execute(
        "UPDATE tag_demande SET statut = %s, decide_par = %s, decide_le = now() WHERE id = %s",
        (payload.statut, user.id, demande_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "decision_tag_demande", "tag_demande", demande_id, {"statut": payload.statut})
    return {"id": demande_id, "statut": payload.statut, "tag_id": tag_id}


class EvenementTagsIn(BaseModel):
    tag_ids: list[ShortStr] = []


@router.put("/pilotage/activites/{evenement_id}/tags")
def taguer_activite(evenement_id: str, payload: EvenementTagsIn, ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, object]:
    """Attach catalogue tags to an activity the caller may manage (its target must
    be inside the caller's scope). Replaces the current tag set."""
    ev = db.fetch_one("SELECT cible_type, cible_id FROM evenement WHERE id = %s", (evenement_id,), role=ctx.user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activite introuvable")
    cible_type = str(ev["cible_type"] or "general")
    cible_id = str(ev["cible_id"]) if ev.get("cible_id") else None
    autorise = ctx.scope.is_global if cible_type == "general" else ctx.scope.couvre(cible_type, cible_id)
    if not autorise:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="activite hors de votre perimetre")
    db.execute("DELETE FROM evenement_tag WHERE evenement_id = %s", (evenement_id,), role=ctx.user.role)
    for tid in payload.tag_ids:
        db.execute(
            "INSERT INTO evenement_tag (evenement_id, tag_id) SELECT %s, %s WHERE EXISTS (SELECT 1 FROM tag WHERE id = %s AND actif = true) ON CONFLICT DO NOTHING",
            (evenement_id, tid, tid),
            role=ctx.user.role,
        )
    return {"ok": True, "tags": len(payload.tag_ids)}
