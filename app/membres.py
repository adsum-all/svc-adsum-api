"""Member-facing endpoints, served from the real PostgreSQL.

Every query runs under the caller role, which activates the per-role RLS
policies (ADR-0002). Results are additionally scoped to the authenticated
member by ``membre_id`` so a member only ever reads their own records.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import db
from .auth import current_user
from .mappers import MEMBRE_PROFILE_FROM, MEMBRE_PROFILE_SELECT, membre_row_to_profile
from .qr import QrSigningUnavailable, issue_token
from .schemas import (
    ChangePasswordIn,
    DocumentOut,
    DocumentSubmitIn,
    EngagementAcceptIn,
    EngagementOut,
    EvenementOut,
    MembreProfile,
    NotificationOut,
    ParticipationIn,
    PresenceOut,
    QrToken,
    RecensementOut,
    RecensementReponseIn,
    UserMe,
)
from .security import hash_password, verify_password

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
        f"SELECT {MEMBRE_PROFILE_SELECT} {MEMBRE_PROFILE_FROM} WHERE m.id = %s",
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


@router.get("/me/documents", response_model=list[DocumentOut])
def my_documents(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[DocumentOut]:
    """The member's verification dossier: each requested or received piece and its status."""
    membre_id, role = ctx
    rows = db.fetch_all(
        """
        SELECT id, type, statut, demande_le, recu_le, traite_le
        FROM document
        WHERE membre_id = %s
        ORDER BY demande_le DESC NULLS LAST
        """,
        (membre_id,),
        role=role,
    )
    return [
        DocumentOut(
            id=str(r["id"]),
            type=r["type"],
            statut=r["statut"],
            demande_le=r["demande_le"],
            recu_le=r["recu_le"],
            traite_le=r["traite_le"],
        )
        for r in rows
    ]


@router.get("/me/engagements", response_model=list[EngagementOut])
def my_engagements(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[EngagementOut]:
    """The engagements the member has signed, or that remain to be signed."""
    membre_id, role = ctx
    rows = db.fetch_all(
        """
        SELECT id, type, version, signe_le
        FROM engagement
        WHERE membre_id = %s
        ORDER BY signe_le DESC NULLS LAST
        """,
        (membre_id,),
        role=role,
    )
    return [
        EngagementOut(
            id=str(r["id"]),
            type=r["type"],
            version=r["version"],
            signe=r["signe_le"] is not None,
            signe_le=r["signe_le"],
        )
        for r in rows
    ]


@router.get("/me/recensement", response_model=RecensementOut | None)
def my_recensement(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> RecensementOut | None:
    membre_id, role = ctx
    row = db.fetch_one(
        """
        SELECT r.id, r.annee, r.statut,
               EXISTS (
                   SELECT 1 FROM recensement_reponse rr
                   WHERE rr.recensement_id = r.id AND rr.membre_id = %s
               ) AS deja_repondu
        FROM recensement r
        WHERE r.statut = 'ouvert'
        ORDER BY r.annee DESC
        LIMIT 1
        """,
        (membre_id,),
        role=role,
    )
    if not row:
        return None
    return RecensementOut(
        id=str(row["id"]),
        annee=int(row["annee"]),
        statut=row["statut"],
        ouvert=row["statut"] == "ouvert",
        deja_repondu=bool(row["deja_repondu"]),
    )


@router.post("/me/recensement", status_code=status.HTTP_201_CREATED)
def submit_recensement(
    payload: RecensementReponseIn,
    ctx: Annotated[tuple[str, str], Depends(require_membre)],
) -> dict[str, object]:
    membre_id, role = ctx
    recensement = db.fetch_one(
        "SELECT id FROM recensement WHERE statut = 'ouvert' ORDER BY annee DESC LIMIT 1",
        (),
        role=role,
    )
    if not recensement:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no open census")
    db.execute(
        """
        INSERT INTO recensement_reponse (recensement_id, membre_id, reponses, soumis_le)
        VALUES (%s, %s, %s::jsonb, now())
        ON CONFLICT (recensement_id, membre_id)
        DO UPDATE SET reponses = EXCLUDED.reponses, soumis_le = now()
        """,
        (str(recensement["id"]), membre_id, json.dumps(payload.model_dump())),
        role=role,
    )
    return {"ok": True}


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordIn, user: Annotated[UserMe, Depends(current_user)]
) -> None:
    """Change the member's own password after verifying the current one."""
    if len(payload.nouveau) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short")
    row = db.fetch_one("SELECT hash_mdp FROM utilisateur WHERE id = %s", (user.id,), role=user.role)
    if not row or not verify_password(payload.ancien, str(row["hash_mdp"])):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="current password invalid")
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s WHERE id = %s",
        (hash_password(payload.nouveau), user.id),
        role=user.role,
    )


@router.post("/me/engagements/accepter", response_model=EngagementOut, status_code=status.HTTP_201_CREATED)
def accept_engagement(
    payload: EngagementAcceptIn, ctx: Annotated[tuple[str, str], Depends(require_membre)]
) -> EngagementOut:
    """Record the member's signed acceptance of an engagement (consent proof)."""
    membre_id, role = ctx
    created = db.execute(
        """
        INSERT INTO engagement (membre_id, type, version, signe_le, hash_preuve)
        VALUES (%s, %s, %s, now(), md5(%s || now()::text))
        RETURNING id, type, version, signe_le
        """,
        (membre_id, payload.type, payload.version, membre_id),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="engagement not recorded")
    return EngagementOut(
        id=str(created["id"]),
        type=created["type"],
        version=created["version"],
        signe=created["signe_le"] is not None,
        signe_le=created["signe_le"],
    )


@router.post("/me/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def submit_document(
    payload: DocumentSubmitIn, ctx: Annotated[tuple[str, str], Depends(require_membre)]
) -> DocumentOut:
    """Register a member-submitted document (metadata); the file pipeline is separate."""
    membre_id, role = ctx
    created = db.execute(
        """
        INSERT INTO document (membre_id, type, statut, demande_le, recu_le)
        VALUES (%s, %s, 'recu', now(), now())
        RETURNING id, type, statut, demande_le, recu_le, traite_le
        """,
        (membre_id, payload.type),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="document not registered")
    return DocumentOut(
        id=str(created["id"]),
        type=created["type"],
        statut=created["statut"],
        demande_le=created["demande_le"],
        recu_le=created["recu_le"],
        traite_le=created["traite_le"],
    )


@router.post("/me/participation", status_code=status.HTTP_201_CREATED)
def participer_session(
    payload: ParticipationIn, ctx: Annotated[tuple[str, str], Depends(require_membre)]
) -> dict[str, object]:
    """Validate an online session participation, captured as an attendance record."""
    membre_id, role = ctx
    db.execute(
        """
        INSERT INTO presence (membre_id, evenement_id, mode, arrivee, methode)
        VALUES (%s, %s, 'en_ligne', now(), 'lien')
        ON CONFLICT (membre_id, evenement_id)
        DO UPDATE SET depart = now()
        """,
        (membre_id, payload.evenement_id),
        role=role,
    )
    return {"ok": True}


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
