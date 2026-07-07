"""Member-facing endpoints, served from the real PostgreSQL.

Every query sets the caller role for the per-role RLS policies (ADR-0002), which
act as defense in depth: the backend connects as the table owner, which bypasses
RLS. What actually scopes a member to their own records is the explicit
``membre_id`` filter on every query, not RLS. For events, the shared targeting
predicate in ``visibilite`` gates both the agenda and the action endpoints.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db, visibilite
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


class FuseauIn(BaseModel):
    """The member's IANA time zone, detected client-side (e.g. 'Europe/Paris')."""

    fuseau: str


def _notify(
    membre_id: str,
    role: str,
    type_: str,
    titre: str,
    corps: str,
    email: str | None = None,
) -> None:
    """Create a real in-app notification and, when an e-mail is known, send it too."""
    db.execute(
        """
        INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le)
        VALUES (%s, %s, %s, %s, false, now())
        """,
        (membre_id, type_, titre, corps),
        role=role,
    )
    target = email
    if target is None:
        row = db.fetch_one("SELECT email FROM utilisateur WHERE membre_id = %s", (membre_id,), role=role)
        target = str(row["email"]) if row and row.get("email") else None
    if target:
        try:
            from .email_gateway import send_notification

            send_notification(target, titre, corps)
        except Exception:
            pass


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
    from . import fonctions_membre

    fonctions = fonctions_membre.fonctions_publiques(membre_id, row.get("genre"), role)
    return membre_row_to_profile(row, fonctions)


@router.get("/me/permissions")
def my_permissions(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """The atomic permissions the signed-in account effectively holds.

    Powers the back office: the menu shows only what the account can do and the
    login accepts an account (even role 'membre') that holds at least one back
    office permission through a specialized group. This is a convenience mirror,
    never a security barrier; every endpoint is still guarded server side by
    ``require_permission``.
    """
    from .permissions_rbac import permissions_effectives

    perms = sorted(permissions_effectives(user))
    # 'membres.self' is the baseline every account holds; back-office reach means
    # holding at least one permission beyond it.
    back_office = [p for p in perms if p != "membres.self"]
    return {"role": user.role, "permissions": perms, "acces_back_office": bool(back_office)}


@router.put("/me/fuseau", status_code=status.HTTP_204_NO_CONTENT)
def set_fuseau(payload: FuseauIn, ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> None:
    """Store the member's IANA time zone, detected by their device on app open.

    Used to localize server-rendered times (Telegram, e-mail, survey reminders) to
    the member's own zone. Only a real IANA identifier is accepted, never a fixed
    offset, so seasonal (DST) transitions stay correct.
    """
    from . import temps

    membre_id, role = ctx
    zone = temps.zone_valide(payload.fuseau)
    if not zone:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fuseau horaire invalide")
    db.execute("UPDATE membre SET fuseau_horaire = %s WHERE id = %s", (zone, membre_id), role=role)


@router.get("/me/evenements", response_model=list[EvenementOut])
def my_events(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> list[EvenementOut]:
    membre_id, role = ctx
    # Lifecycle is computed from the server clock, never from a manual flag: a
    # future event can never look joinable, and a stored link stays hidden until
    # the joinable window opens (15 min before the start, until the end + 6 h).
    rows = db.fetch_all(
        """
        SELECT e.id, e.titre, e.type, e.volet, e.debut, e.fin, e.lieu, e.mode,
               e.session_ouverte, e.lien_session, e.liens, e.type_diffusion, e.visibilite,
               e.cible_type, e.cible_id,
               CASE e.cible_type
                 WHEN 'coordination' THEN (SELECT nom FROM coordination WHERE id = e.cible_id)
                 WHEN 'commission' THEN (SELECT nom FROM commission WHERE id = e.cible_id)
                 WHEN 'intendance' THEN (SELECT nom FROM intendance WHERE id = e.cible_id)
                 WHEN 'tribu' THEN (SELECT nom FROM tribu WHERE id = e.cible_id)
                 ELSE NULL
               END AS cible_libelle,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.id::text, 'cle', t.cle, 'libelle', t.libelle) ORDER BY t.libelle)
                         FROM evenement_tag et JOIN tag t ON t.id = et.tag_id WHERE et.evenement_id = e.id), '[]'::jsonb) AS tags,
               EXISTS (SELECT 1 FROM participation p WHERE p.evenement_id = e.id AND p.membre_id = %s) AS inscrit,
               CASE
                 WHEN now() < e.debut - interval '15 minutes' THEN 'a_venir'
                 WHEN now() < e.debut THEN 'bientot'
                 WHEN now() <= COALESCE(e.fin, e.debut + interval '6 hours') THEN 'en_cours'
                 ELSE 'termine'
               END AS phase,
               (now() >= e.debut) AS formulaire_ouvert,
               (now() >= e.debut - interval '15 minutes'
                AND now() <= COALESCE(e.fin, e.debut + interval '6 hours')) AS in_window
        FROM evenement e
        JOIN membre m ON m.id = %s
        LEFT JOIN intendance mi ON mi.id = m.intendance_id
        WHERE e.debut >= now() - interval '30 days'
          AND """ + visibilite.CIBLE_PREDICATE + """
        ORDER BY e.debut ASC
        LIMIT 100
        """,
        (membre_id, membre_id),
        role=role,
    )
    out: list[EvenementOut] = []
    for r in rows:
        liens = _visible_links(r)
        out.append(
            EvenementOut(
                id=str(r["id"]),
                titre=r["titre"],
                type=r["type"],
                volet=r["volet"],
                debut=r["debut"],
                fin=r["fin"],
                lieu=r["lieu"],
                mode=r["mode"],
                session_ouverte=r["session_ouverte"],
                lien_session=liens[0] if liens else None,
                liens=liens,
                type_diffusion=r["type_diffusion"] or "aucun",
                visibilite=r["visibilite"] or "membres",
                cible_type=r.get("cible_type") or "general",
                cible_id=str(r["cible_id"]) if r.get("cible_id") else None,
                cible_libelle=r.get("cible_libelle"),
                tags=[{"id": str(x.get("id")), "cle": str(x.get("cle") or ""), "libelle": str(x.get("libelle") or "")} for x in (r.get("tags") or [])],
                phase=str(r["phase"]),
                joignable=bool(liens),
                formulaire_ouvert=bool(r["formulaire_ouvert"]),
            )
        )
    return out


def _visible_links(r: dict[str, object]) -> list[str]:
    # Links are only shown inside the joinable time window (never merely because
    # they were stored), and a private event additionally requires registration.
    if not r["in_window"]:
        return []
    if r["visibilite"] == "prive" and not r.get("inscrit"):
        return []
    liens = [str(x) for x in (r.get("liens") or []) if x]
    if not liens and r.get("lien_session"):
        liens = [str(r["lien_session"])]
    return liens


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


@router.post("/me/notifications/lire", status_code=status.HTTP_204_NO_CONTENT)
def mark_notifications_read(ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> None:
    """Mark all of the member's notifications as read."""
    membre_id, role = ctx
    db.execute(
        "UPDATE notification SET lu = true, lu_le = coalesce(lu_le, now()) WHERE membre_id = %s AND NOT lu",
        (membre_id,),
        role=role,
    )


# Registered after the literal /lire route on purpose: the literal path must
# win over the parameterized one.
@router.post("/me/notifications/{notification_id}/lire", status_code=status.HTTP_204_NO_CONTENT)
def mark_notification_read(notification_id: str, ctx: Annotated[tuple[str, str], Depends(require_membre)]) -> None:
    """Mark ONE notification as read: persistent, timestamped and idempotent
    (repeated clicks never move the recorded reading time)."""
    membre_id, role = ctx
    db.execute(
        "UPDATE notification SET lu = true, lu_le = coalesce(lu_le, now()) WHERE id = %s AND membre_id = %s",
        (notification_id, membre_id),
        role=role,
    )


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
    # Revoke every session so a stolen token cannot outlive the change, and audit
    # the event (never the password value). The caller re-authenticates afterwards.
    db.execute(
        "UPDATE session SET fin = now(), revoque = true WHERE utilisateur_id = %s AND fin IS NULL",
        (user.id,),
        role=user.role,
    )
    from . import audit

    audit.log(user.id, user.role, "changement_mdp", "utilisateur", user.id, {"sessions_revoquees": True})


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
    _notify(
        membre_id,
        role,
        "engagement",
        "Engagement signé",
        f"Votre engagement « {payload.type.replace('_', ' ')} » a bien été enregistré.",
    )
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
    _notify(
        membre_id,
        role,
        "document",
        "Document reçu",
        "Votre document a bien été reçu. Il sera examiné par l'administration.",
    )
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
    if not visibilite.evenement_visible_membre(payload.evenement_id, membre_id, role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
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
    _notify(
        membre_id,
        role,
        "participation",
        "Participation enregistrée",
        "Votre participation à la session en ligne a bien été enregistrée. Merci.",
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
