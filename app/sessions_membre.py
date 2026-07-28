"""Let a member see where their account is signed in, and close what is not theirs.

The application recorded every session from the start, with its address, device,
country, city and region, but never showed any of it back. The member space said the
opposite: that connection tracking would arrive "once it is in service on the server
side". It has been in service the whole time, so a member who wanted to check whether
somebody else was using their account had no way to find out, and the interface told
them the feature did not exist.

This module gives them the ledger that was already being written, plus the one action
that makes it useful: closing every session except the one they are holding.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["securite"])


def _lieu(r: dict[str, Any]) -> str | None:
    """City, region and country as one readable line, skipping what is unknown."""
    parties = [str(r.get(k) or "").strip() for k in ("ville", "region", "pays")]
    retenues = [p for p in parties if p]
    return ", ".join(retenues) or None


@router.get("/connexions")
def mes_connexions(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """The member's own sessions, most recent first, with the current one marked.

    Scoped to the caller's account by construction: the identifier comes from the
    verified token, never from the request, so a member can only ever read their own.
    """
    total = int((db.fetch_one(
        "SELECT COUNT(*) AS n FROM session WHERE utilisateur_id = %s", (user.id,), role=user.role,
    ) or {}).get("n") or 0)
    lignes = db.fetch_all(
        "SELECT id, ip::text AS ip, appareil, pays, ville, region, cree_le, fin, revoque "
        "FROM session WHERE utilisateur_id = %s ORDER BY cree_le DESC LIMIT %s OFFSET %s",
        (user.id, taille, (page - 1) * taille), role=user.role,
    )
    courante = str(user.session_id or "")
    items = []
    for r in lignes:
        sid = str(r["id"])
        ouverte = r.get("fin") is None and not r.get("revoque")
        items.append({
            "id": sid,
            "courante": bool(courante) and sid == courante,
            "ouverte": ouverte,
            "appareil": r.get("appareil") or None,
            # The address is shown to its owner only, so they can recognise a
            # connection that is not theirs. Nobody else can read this route.
            "adresse": r.get("ip"),
            "lieu": _lieu(r),
            "ouverte_le": r.get("cree_le"),
            "fermee_le": r.get("fin"),
            "revoquee": bool(r.get("revoque")),
        })
    ouvertes = int((db.fetch_one(
        "SELECT COUNT(*) AS n FROM session WHERE utilisateur_id = %s AND fin IS NULL AND revoque = false",
        (user.id,), role=user.role,
    ) or {}).get("n") or 0)
    return {
        "items": items, "total": total, "page": page, "taille": taille,
        "pages": max(1, (total + taille - 1) // taille),
        "ouvertes": ouvertes,
        # Only worth offering the action when there is something else to close.
        "autres_ouvertes": max(0, ouvertes - (1 if courante else 0)),
    }


@router.post("/connexions/fermer-autres")
def fermer_autres_connexions(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, Any]:
    """Close every session except the one making the request.

    The current session is spared deliberately: a member acting on a suspicion should
    not be signed out by their own precaution, which would leave them unable to change
    their password straight away.
    """
    courante = str(user.session_id or "")
    if courante:
        fermees = db.fetch_all(
            "UPDATE session SET fin = now(), revoque = true "
            "WHERE utilisateur_id = %s AND id <> %s AND fin IS NULL RETURNING id",
            (user.id, courante), role=user.role,
        )
    else:
        fermees = db.fetch_all(
            "UPDATE session SET fin = now(), revoque = true "
            "WHERE utilisateur_id = %s AND fin IS NULL RETURNING id",
            (user.id,), role=user.role,
        )
    audit.log(user.id, user.role, "sessions_autres_fermees", "session", courante or None,
              {"fermees": len(fermees)})
    return {"fermees": len(fermees)}
