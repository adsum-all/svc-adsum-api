# ruff: noqa: E501
"""Paginated, filterable, searchable member feed for the Informations tab.

Extracted from ``information.py`` to keep each module cohesive and under the size
threshold. Reuses the shared feed SELECT, serialiser and access guard from that
module. Its router MUST be included BEFORE the ``information`` router in ``main.py``
so the literal ``/informations/feed`` path wins over ``/informations/{info_id}``.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from . import db
from .auth import current_user
from .information import _FEED_SELECT, _info_dict, _membre_ou_403
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["informations"])


@router.get("/membres/me/informations/prioritaires")
def informations_prioritaires(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, Any]]:
    """Active high-priority Informations for the discreet open-app banner: urgent or
    important, still within their display window, not yet read. At most three,
    urgent first. The member app shows one at a time (dismissed once per member)."""
    mid = _membre_ou_403(user)
    rows = db.fetch_all(
        _FEED_SELECT
        + " AND i.priorite IN ('urgente','importante') AND d.statut NOT IN ('lu','confirme')"
        + " ORDER BY (i.priorite = 'urgente') DESC, (i.epingle_jusqu IS NOT NULL AND i.epingle_jusqu > now()) DESC, i.envoye_le DESC LIMIT 3",
        (mid,), role=user.role,
    )
    out = []
    for r in rows:
        out.append({
            "id": str(r["id"]), "titre": r.get("titre"), "sous_titre": r.get("sous_titre"),
            "priorite": r.get("priorite"), "expire_le": r["expire_le"].isoformat() if r.get("expire_le") else None,
        })
    return out


@router.get("/membres/me/informations/feed")
def feed_membre_pagine(
    user: Annotated[UserMe, Depends(current_user)],
    priorite: str | None = None,
    lecture: str | None = None,
    q: str | None = None,
    mois: int | None = None,
    annee: int | None = None,
    type_contenu: str | None = None,
    auteur: str | None = None,
    limit: int = 15,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated, filterable, searchable member feed. Ordering keeps active items
    (urgent, then pinned, then important) on top; the client groups the rest by
    month so a multi-year history never loads at once."""
    mid = _membre_ou_403(user)
    limit = max(1, min(50, limit))
    offset = max(0, offset)
    where = _FEED_SELECT
    params: list[Any] = [mid]
    if priorite in ("normale", "importante", "urgente"):
        where += " AND i.priorite = %s"
        params.append(priorite)
    if lecture == "non_lu":
        where += " AND d.statut NOT IN ('lu','confirme')"
    elif lecture == "lu":
        where += " AND d.statut IN ('lu','confirme')"
    if q:
        where += " AND (i.titre ILIKE %s OR i.contenu ILIKE %s OR i.auteur ILIKE %s)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if mois:
        where += " AND EXTRACT(MONTH FROM i.envoye_le) = %s"
        params.append(int(mois))
    if annee:
        where += " AND EXTRACT(YEAR FROM i.envoye_le) = %s"
        params.append(int(annee))
    if auteur:
        where += " AND i.auteur ILIKE %s"
        params.append(f"%{auteur}%")
    if type_contenu == "audio":
        where += " AND i.audio_url IS NOT NULL"
    elif type_contenu == "document":
        where += " AND i.document_url IS NOT NULL"
    elif type_contenu == "lien":
        where += " AND (i.lien_url IS NOT NULL OR i.action_url IS NOT NULL)"
    elif type_contenu == "texte":
        where += " AND i.audio_url IS NULL AND i.document_url IS NULL AND i.image_url IS NULL"

    total_row = db.fetch_one(f"SELECT count(*) AS c FROM ({where}) s", tuple(params), role=user.role)
    total = int(total_row["c"]) if total_row else 0
    rows = db.fetch_all(
        where + " ORDER BY (i.priorite = 'urgente') DESC, "
        "(i.epingle_jusqu IS NOT NULL AND i.epingle_jusqu > now()) DESC, "
        "(i.priorite = 'importante') DESC, i.envoye_le DESC LIMIT %s OFFSET %s",
        (*params, limit, offset), role=user.role,
    )
    items = []
    for r in rows:
        d = _info_dict(r)
        d.pop("cibles", None)
        d.update({"lu": r.get("d_statut") in ("lu", "confirme"), "confirme": r.get("d_statut") == "confirme",
                  "lu_le": r.get("lu_le"), "confirme_le": r.get("confirme_le")})
        items.append(d)
    pages = max(1, (total + limit - 1) // limit)
    return {"items": items, "total": total, "page": (offset // limit) + 1, "pages": pages, "limit": limit}
