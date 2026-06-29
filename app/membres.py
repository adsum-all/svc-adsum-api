"""Member-facing endpoints, served from the real PostgreSQL.

Every query runs under the caller role, which activates the per-role RLS
policies (ADR-0002). Results are additionally scoped to the authenticated
member by ``membre_id`` so a member only ever reads their own records.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import db
from .auth import current_user
from .mappers import MEMBRE_PROFILE_SELECT, membre_row_to_profile
from .qr import QrSigningUnavailable, issue_token
from .schemas import EvenementOut, MembreProfile, NotificationOut, PresenceOut, QrToken, UserMe

router = APIRouter(prefix="/api/v1/membres", tags=["membres"])


def require_membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    """Return ``(membre_id, role)`` for an account linked to a member.

    Raises HTTP 403 when the authenticated account is not bound to a member.
    """
    if not user.membre_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account is not linked to a member",
        )
    return user.membre_id, user.role


@router.get("/me", response_model=MembreProfile)
def my_profile(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> MembreProfile:
    membre_id, role = ctx
    row = db.fetch_one(
        f"""
        SELECT {MEMBRE_PROFILE_SELECT}
        FROM membre m
        LEFT JOIN commission c ON c.id = m.commission_id
        WHERE m.id = %s
        """,
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return membre_row_to_profile(row)


@router.get("/me/evenements", response_model=list[EvenementOut])
def my_events(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[EvenementOut]:
    _, role = ctx
    rows = db.fetch_all(
        """
        SELECT id, titre, type, volet, debut, fin, lieu, session_ouverte
        FROM evenement
        WHERE fin IS NULL OR fin >= now()
        ORDER BY debut ASC
        LIMIT 100
        """,
        (),
        role=role,
    )
    return [
        EvenementOut(
            id=str(r["id"]),
            titre=r["titre"],
            type=r["type"],
            volet=r["volet"],
            debut=r["debut"],
            fin=r["fin"],
            lieu=r["lieu"],
            session_ouverte=r["session_ouverte"],
        )
        for r in rows
    ]


@router.get("/me/historique", response_model=list[PresenceOut])
def my_history(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[PresenceOut]:
    membre_id, role = ctx
    rows = db.fetch_all(
        """
        SELECT p.evenement_id, e.titre AS evenement_titre, e.debut,
               p.arrivee, p.depart, p.methode
        FROM presence p
        JOIN evenement e ON e.id = p.evenement_id
        WHERE p.membre_id = %s
        ORDER BY p.arrivee DESC NULLS LAST
        LIMIT 200
        """,
        (membre_id,),
        role=role,
    )
    return [
        PresenceOut(
            evenement_id=str(r["evenement_id"]),
            evenement_titre=r["evenement_titre"],
            debut=r["debut"],
            arrivee=r["arrivee"],
            depart=r["depart"],
            methode=r["methode"],
        )
        for r in rows
    ]


@router.get("/me/notifications", response_model=list[NotificationOut])
def my_notifications(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[NotificationOut]:
    membre_id, role = ctx
    rows = db.fetch_all(
        """
        SELECT id, type, titre, corps, lu, cree_le
        FROM notification
        WHERE membre_id = %s
        ORDER BY cree_le DESC NULLS LAST
        LIMIT 100
        """,
        (membre_id,),
        role=role,
    )
    return [
        NotificationOut(
            id=str(r["id"]),
            type=r["type"],
            titre=r["titre"],
            corps=r["corps"],
            lu=r["lu"],
            cree_le=r["cree_le"],
        )
        for r in rows
    ]


@router.get("/me/qr", response_model=QrToken)
def my_qr(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> QrToken:
    membre_id, _ = ctx
    try:
        issued = issue_token(membre_id)
    except QrSigningUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="QR signing is not configured on this server",
        ) from exc
    return QrToken(
        token=str(issued["token"]),
        membre_id=membre_id,
        issued_at=datetime.fromtimestamp(int(issued["issued_at"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(issued["expires_at"]), tz=UTC),
        key_version=int(issued["key_version"]),
    )
