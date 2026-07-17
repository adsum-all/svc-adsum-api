"""Volet B counting: members scanned plus non-members, with a public self-service.

For large public events (volet B), members are scanned (presence) and non-members
are counted manually or self-declare through a public anonymous QR. The total is
members + non-members. The public endpoint requires no authentication.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from . import audit, db, ratelimit
from .permissions_rbac import require_permission
from .schemas import (
    ComptageLigne,
    ComptageResume,
    CreateComptage,
    PublicEvent,
    UserMe,
)

router = APIRouter(prefix="/api/v1/admin/comptage", tags=["comptage"])
comptage_public_router = APIRouter(prefix="/api/v1/public", tags=["public"])



def _resume(evenement_id: str, role: str | None) -> ComptageResume:
    event = db.fetch_one("SELECT titre FROM evenement WHERE id = %s", (evenement_id,), role=role)
    # Members physically scanned at the venue only (QR or manual check-in). Online
    # link participations (methode='lien') are not on-site attendees, so they must
    # not inflate the volet B physical count.
    membres = db.fetch_one(
        "SELECT count(*) AS n FROM presence WHERE evenement_id = %s AND methode IN ('qr', 'manuelle')",
        (evenement_id,),
        role=role,
    )
    rows = db.fetch_all(
        """
        SELECT id, segment, total_membres, total_anonyme, horodatage
        FROM comptage_volet_b
        WHERE evenement_id = %s
        ORDER BY horodatage ASC
        """,
        (evenement_id,),
        role=role,
    )
    non_membres = sum(int(r["total_anonyme"]) for r in rows)
    # Members counted manually on segments that were not scanned (large venues), so
    # they are no longer silently dropped from the total.
    membres_manuels = sum(int(r["total_membres"] or 0) for r in rows)
    membres_scannes = int(membres["n"]) if membres else 0
    return ComptageResume(
        evenement_id=evenement_id,
        titre=event["titre"] if event else None,
        membres_scannes=membres_scannes,
        membres_comptes_manuellement=membres_manuels,
        non_membres=non_membres,
        total_participants=membres_scannes + membres_manuels + non_membres,
        lignes=[
            ComptageLigne(
                id=str(r["id"]),
                segment=r["segment"] if isinstance(r["segment"], str) else None,
                total_membres=int(r["total_membres"]),
                total_anonyme=int(r["total_anonyme"]),
                horodatage=r["horodatage"],
            )
            for r in rows
        ],
    )


class EvenementEligible(BaseModel):
    id: str
    titre: str
    debut: str | None = None
    lieu: str | None = None
    annule: bool = False


class EvenementsEligiblesOut(BaseModel):
    items: list[EvenementEligible]
    total: int


# Declared before the /{evenement_id} route so the literal path is not captured as an id.
@router.get("/evenements-eligibles", response_model=EvenementsEligiblesOut)
def evenements_eligibles(
    user: Annotated[UserMe, Depends(require_permission("comptage.superviser"))],
    q: str | None = Query(default=None),
    periode: str = Query(default="pertinents", pattern="^(pertinents|a_venir|passes|tous)$"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EvenementsEligiblesOut:
    """Server-side, filtered and paginated list of the events eligible for volet B counting
    (volet 'B' and not cancelled), so the selector never downloads the whole event history
    to the browser and stays fast at any scale. Filter by free text (title or place) and by
    period ('pertinents' = the last 90 days and everything upcoming, by default)."""
    where = ["e.volet = 'B'", "e.annule = false"]
    params: list[object] = []
    if q and q.strip():
        where.append("(e.titre ILIKE %s OR coalesce(e.lieu, '') ILIKE %s)")
        like = f"%{q.strip()}%"
        params.extend([like, like])
    if periode == "a_venir":
        where.append("e.debut >= now()")
    elif periode == "passes":
        where.append("e.debut < now()")
    elif periode == "pertinents":
        where.append("(e.debut IS NULL OR e.debut >= now() - interval '90 days')")
    clause = " AND ".join(where)
    total_row = db.fetch_one(f"SELECT count(*) AS n FROM evenement e WHERE {clause}", tuple(params), role=user.role)
    total = int((total_row or {}).get("n", 0))
    rows = db.fetch_all(
        f"SELECT e.id, e.titre, e.debut, e.lieu, e.annule FROM evenement e WHERE {clause} "
        "ORDER BY e.debut DESC NULLS LAST LIMIT %s OFFSET %s",
        (*params, limit, offset),
        role=user.role,
    )
    items = [
        EvenementEligible(
            id=str(r["id"]),
            titre=str(r["titre"]),
            debut=r["debut"].isoformat() if r.get("debut") else None,
            lieu=r.get("lieu"),
            annule=bool(r.get("annule")),
        )
        for r in rows
    ]
    return EvenementsEligiblesOut(items=items, total=total)


@router.get("/{evenement_id}", response_model=ComptageResume)
def get_comptage(
    evenement_id: str,
    user: Annotated[UserMe, Depends(require_permission("comptage.superviser"))],
) -> ComptageResume:
    return _resume(evenement_id, user.role)


@router.post("", response_model=ComptageResume, status_code=status.HTTP_201_CREATED)
def add_comptage(
    payload: CreateComptage,
    user: Annotated[UserMe, Depends(require_permission("comptage.controler"))],
) -> ComptageResume:
    db.execute(
        """
        INSERT INTO comptage_volet_b (evenement_id, segment, total_membres, total_anonyme, saisi_par)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (payload.evenement_id, payload.segment, payload.total_membres, payload.total_anonyme, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "comptage_volet_b", "evenement", payload.evenement_id,
              {"segment": payload.segment, "anonyme": payload.total_anonyme})
    return _resume(payload.evenement_id, user.role)


@comptage_public_router.get("/evenement/{evenement_id}", response_model=PublicEvent)
def public_event(evenement_id: str) -> PublicEvent:
    row = db.fetch_one(
        "SELECT id, titre, volet, visibilite, session_ouverte, lien_session, type_diffusion "
        "FROM evenement WHERE id = %s",
        (evenement_id,),
    )
    if not row or (row["volet"] != "B" and row["visibilite"] != "public"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="public event not found")
    ouvert = bool(row["session_ouverte"])
    # A public broadcast link is only served while the session is open.
    public_live = row["visibilite"] == "public" and ouvert
    return PublicEvent(
        id=str(row["id"]),
        titre=str(row["titre"]),
        ouvert=ouvert,
        lien_session=row["lien_session"] if public_live else None,
        type_diffusion=(row["type_diffusion"] or "aucun") if public_live else "aucun",
    )


@comptage_public_router.post("/presence/{evenement_id}", status_code=status.HTTP_201_CREATED)
def public_presence(evenement_id: str, request: Request) -> dict[str, object]:
    """Anonymous self-service presence for a volet B event ('Je suis present').

    Rate limited per client: an anonymous public counter cannot be truly
    deduplicated, so throttling caps how fast a refresh, a double click or a script
    can inflate the tally. The figure stays an approximate anonymous headcount."""
    ratelimit.enforce(request, "public-comptage")
    row = db.fetch_one("SELECT id, volet FROM evenement WHERE id = %s", (evenement_id,))
    if not row or row["volet"] != "B":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="public event not found")
    db.execute(
        """
        INSERT INTO comptage_volet_b (evenement_id, segment, total_anonyme)
        VALUES (%s, 'self-service', 1)
        """,
        (evenement_id,),
    )
    return {"ok": True, "evenement_id": evenement_id}
