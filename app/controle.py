"""Field control endpoints (QR verify and check-in), on the real PostgreSQL.

Reserved to staff roles that operate attendance. Tokens are verified offline
with the published Ed25519 public key, then attendance is written under the
caller role so the per-role RLS policies (ADR-0002) still apply.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import db
from .deps import require_roles
from .qr import verify_token
from .schemas import (
    CheckinMembre,
    CheckinRequest,
    CheckinResult,
    EvenementOut,
    UserMe,
    VerifyRequest,
    VerifyResult,
)

router = APIRouter(prefix="/api/v1/controle", tags=["controle"])

CONTROL_ROLES = ("super_admin", "admin", "gestionnaire", "controleur")
require_control = require_roles(*CONTROL_ROLES)


def _lookup_membre(membre_id: str, role: str) -> dict[str, object] | None:
    return db.fetch_one(
        "SELECT id, matricule, nom, prenoms FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )


@router.get("/evenements", response_model=list[EvenementOut])
def open_or_upcoming_events(
    user: Annotated[UserMe, Depends(require_control)],
) -> list[EvenementOut]:
    rows = db.fetch_all(
        """
        SELECT id, titre, type, volet, debut, fin, lieu, session_ouverte
        FROM evenement
        WHERE fin IS NULL OR fin >= now()
        ORDER BY debut ASC
        LIMIT 200
        """,
        (),
        role=user.role,
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


@router.post("/verify", response_model=VerifyResult)
def verify(
    payload: VerifyRequest,
    user: Annotated[UserMe, Depends(require_control)],
) -> VerifyResult:
    result = verify_token(payload.token)
    membre_id = result.get("membre_id")
    matricule = nom = prenoms = None
    if isinstance(membre_id, str):
        row = _lookup_membre(membre_id, user.role)
        if row:
            matricule = row["matricule"]
            nom = row["nom"]
            prenoms = row["prenoms"]
    return VerifyResult(
        valid=bool(result["valid"]),
        reason=result.get("reason"),
        membre_id=membre_id if isinstance(membre_id, str) else None,
        issued_at=result.get("issued_at"),
        expires_at=result.get("expires_at"),
        key_version=result.get("key_version"),
        matricule=matricule,
        nom=nom,
        prenoms=prenoms,
    )


@router.post("/checkin", response_model=CheckinResult)
def checkin(
    payload: CheckinRequest,
    user: Annotated[UserMe, Depends(require_control)],
) -> CheckinResult:
    result = verify_token(payload.token)
    if not result["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("reason") or "invalid token"),
        )
    membre_id = result["membre_id"]
    if not isinstance(membre_id, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="malformed payload",
        )
    membre = _lookup_membre(membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    inserted = db.execute(
        """
        INSERT INTO presence (membre_id, evenement_id, mode, arrivee, methode)
        VALUES (%s, %s, 'presentiel', now(), 'qr')
        ON CONFLICT (membre_id, evenement_id) DO NOTHING
        RETURNING arrivee
        """,
        (membre_id, payload.evenement_id),
        role=user.role,
    )
    if inserted:
        arrivee = inserted["arrivee"]
        deja_present = False
    else:
        existing = db.fetch_one(
            "SELECT arrivee FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (membre_id, payload.evenement_id),
            role=user.role,
        )
        arrivee = existing["arrivee"] if existing else None
        deja_present = True
    return CheckinResult(
        deja_present=deja_present,
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
        ),
        evenement_id=payload.evenement_id,
        arrivee=arrivee,
    )
