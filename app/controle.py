"""Field control endpoints (QR verify and check-in), on the real PostgreSQL.

Reserved to staff roles that operate attendance. Tokens are verified offline
with the published Ed25519 public key, then attendance is written under the
caller role so the per-role RLS policies (ADR-0002) still apply.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import db
from .deps import require_roles
from .qr import verify_token
from .schemas import (
    CheckinMembre,
    CheckinRequest,
    CheckinResult,
    CheckoutResult,
    ControlMembre,
    EvenementOut,
    ManualCheckinRequest,
    UserMe,
    VerifyRequest,
    VerifyResult,
)

router = APIRouter(prefix="/api/v1/controle", tags=["controle"])

CONTROL_ROLES = ("super_admin", "admin", "gestionnaire", "controleur")
require_control = require_roles(*CONTROL_ROLES)


def _lookup_membre(membre_id: str, role: str) -> dict[str, object] | None:
    return db.fetch_one(
        "SELECT id, matricule, nom, prenoms, photo_url FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )


def _signed_photo(path: object) -> str | None:
    """Short-lived signed URL for a member's identity photo, for the scan card.
    Best-effort: a signing failure just yields no photo, not an error."""
    if not path:
        return None
    from . import storage
    from .config import settings

    try:
        return storage.signed_download_url(settings.storage_bucket_photos, str(path))
    except storage.StorageError:
        return None


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


@router.get("/membres", response_model=list[ControlMembre])
def directory(
    user: Annotated[UserMe, Depends(require_control)],
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
) -> list[ControlMembre]:
    """Member directory the controller caches for offline lookup and manual entry."""
    where = ""
    params: tuple[object, ...] = ()
    if q:
        where = (
            "WHERE m.matricule ILIKE %s OR m.nom ILIKE %s "
            "OR m.prenoms ILIKE %s OR m.telephone ILIKE %s"
        )
        like = f"%{q}%"
        params = (like, like, like, like)
    rows = db.fetch_all(
        f"""
        SELECT m.id, m.matricule, m.nom, m.prenoms, m.statut, c.nom AS commission
        FROM membre m
        LEFT JOIN commission c ON c.id = m.commission_id
        {where}
        ORDER BY m.matricule ASC
        LIMIT {limit}
        """,
        params,
        role=user.role,
    )
    return [
        ControlMembre(
            id=str(r["id"]),
            matricule=str(r["matricule"]),
            nom=r["nom"] if isinstance(r["nom"], str) else None,
            prenoms=r["prenoms"] if isinstance(r["prenoms"], str) else None,
            commission=r["commission"] if isinstance(r["commission"], str) else None,
            statut=str(r["statut"]),
        )
        for r in rows
    ]


def _mark_present_scan(membre_id: str, evenement_id: str, role: str) -> None:
    """A QR/manual check-in is the source of truth for presence: it upserts a
    validated in-person participation and always wins over a prior declaration."""
    try:
        db.execute(
            """
            INSERT INTO participation (evenement_id, membre_id, statut, source, valide)
            VALUES (%s, %s, 'present', 'scan', true)
            ON CONFLICT (evenement_id, membre_id)
            DO UPDATE SET statut = 'present', source = 'scan', valide = true, maj_le = now()
            """,
            (evenement_id, membre_id),
            role=role,
        )
    except Exception:  # noqa: BLE001 - participation tracking must never block a check-in
        pass


@router.post("/checkin-manuel", response_model=CheckinResult)
def checkin_manuel(
    payload: ManualCheckinRequest,
    user: Annotated[UserMe, Depends(require_control)],
) -> CheckinResult:
    """Manual check-in by member id, logged with the 'manuelle' method (offline fallback)."""
    membre = _lookup_membre(payload.membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    inserted = db.execute(
        """
        INSERT INTO presence (membre_id, evenement_id, mode, arrivee, methode)
        VALUES (%s, %s, 'presentiel', now(), 'manuelle')
        ON CONFLICT (membre_id, evenement_id) DO NOTHING
        RETURNING arrivee
        """,
        (payload.membre_id, payload.evenement_id),
        role=user.role,
    )
    if inserted:
        arrivee = inserted["arrivee"]
        deja_present = False
    else:
        existing = db.fetch_one(
            "SELECT arrivee FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (payload.membre_id, payload.evenement_id),
            role=user.role,
        )
        arrivee = existing["arrivee"] if existing else None
        deja_present = True
    _mark_present_scan(payload.membre_id, payload.evenement_id, user.role)
    return CheckinResult(
        deja_present=deja_present,
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            photo_url=_signed_photo(membre.get("photo_url")),
        ),
        evenement_id=payload.evenement_id,
        arrivee=arrivee,
    )


@router.post("/checkout", response_model=CheckoutResult)
def checkout(
    payload: CheckinRequest,
    user: Annotated[UserMe, Depends(require_control)],
) -> CheckoutResult:
    """Exit mode: a second scan records the member departure for the event."""
    result = verify_token(payload.token)
    if not result["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(result.get("reason") or "invalid token"),
        )
    membre_id = result["membre_id"]
    if not isinstance(membre_id, str):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="malformed payload")
    membre = _lookup_membre(membre_id, user.role)
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    updated = db.execute(
        """
        UPDATE presence SET depart = now()
        WHERE membre_id = %s AND evenement_id = %s AND depart IS NULL
        RETURNING depart
        """,
        (membre_id, payload.evenement_id),
        role=user.role,
    )
    if updated:
        depart = updated["depart"]
        deja_sorti = False
    else:
        existing = db.fetch_one(
            "SELECT depart FROM presence WHERE membre_id = %s AND evenement_id = %s",
            (membre_id, payload.evenement_id),
            role=user.role,
        )
        depart = existing["depart"] if existing else None
        deja_sorti = True
    return CheckoutResult(
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            photo_url=_signed_photo(membre.get("photo_url")),
        ),
        evenement_id=payload.evenement_id,
        depart=depart,
        deja_sorti=deja_sorti,
    )


@router.post("/verify", response_model=VerifyResult)
def verify(
    payload: VerifyRequest,
    user: Annotated[UserMe, Depends(require_control)],
) -> VerifyResult:
    result = verify_token(payload.token)
    membre_id = result.get("membre_id")
    matricule = nom = prenoms = photo_url = None
    if isinstance(membre_id, str):
        row = _lookup_membre(membre_id, user.role)
        if row:
            matricule = row["matricule"]
            nom = row["nom"]
            prenoms = row["prenoms"]
            photo_url = _signed_photo(row.get("photo_url"))
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
        photo_url=photo_url,
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
    _mark_present_scan(membre_id, payload.evenement_id, user.role)
    return CheckinResult(
        deja_present=deja_present,
        membre=CheckinMembre(
            id=str(membre["id"]),
            matricule=str(membre["matricule"]),
            nom=membre["nom"] if isinstance(membre["nom"], str) else None,
            prenoms=membre["prenoms"] if isinstance(membre["prenoms"], str) else None,
            photo_url=_signed_photo(membre.get("photo_url")),
        ),
        evenement_id=payload.evenement_id,
        arrivee=arrivee,
    )
