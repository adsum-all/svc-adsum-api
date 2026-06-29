"""Back-office administration endpoints, served from the real PostgreSQL.

Every query runs under the caller role, which activates the per-role RLS
policies (ADR-0002). Write access is additionally guarded by ``require_roles``
so the API rejects unauthorized callers before touching the database.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import db
from .deps import require_roles
from .mappers import MEMBRE_PROFILE_FROM, MEMBRE_PROFILE_SELECT, membre_row_to_profile
from .schemas import (
    CommissionOut,
    CreateCommission,
    CreateEvenement,
    CreateMembre,
    EvenementOut,
    MembreProfile,
    UpdateMembre,
    UserMe,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

STAFF = ("super_admin", "admin", "gestionnaire", "controleur", "direction")
MEMBRE_WRITERS = ("super_admin", "admin")
EVENT_WRITERS = ("super_admin", "admin", "gestionnaire")

require_staff = require_roles(*STAFF)
require_membre_writer = require_roles(*MEMBRE_WRITERS)
require_event_writer = require_roles(*EVENT_WRITERS)


def _next_matricule(role: str) -> str:
    """Return the next ADS-NNNNNN matricule, one above the current maximum."""
    row = db.fetch_one(
        """
        SELECT COALESCE(MAX(CAST(SUBSTRING(matricule FROM 5) AS integer)), 0) AS last
        FROM membre
        WHERE matricule ~ '^ADS-[0-9]{6}$'
        """,
        (),
        role=role,
    )
    last = row["last"] if row else 0
    return f"ADS-{last + 1:06d}"


def _read_membre(membre_id: str, role: str) -> MembreProfile:
    row = db.fetch_one(
        f"SELECT {MEMBRE_PROFILE_SELECT} {MEMBRE_PROFILE_FROM} WHERE m.id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return membre_row_to_profile(row)


@router.post("/membres", response_model=MembreProfile, status_code=status.HTTP_201_CREATED)
def create_membre(
    payload: CreateMembre,
    user: Annotated[UserMe, Depends(require_membre_writer)],
) -> MembreProfile:
    matricule = payload.matricule or _next_matricule(user.role)
    try:
        created = db.execute(
            """
            INSERT INTO membre (matricule, email, nom, prenoms, telephone, commission_id, groupe,
                                genre, date_naissance, pays, ville, intendance_id, berger_referent_id,
                                date_entree, cheminement_pastoral, statut, verifie)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    COALESCE(%s, 'nouveau'), 'actif', false)
            RETURNING id
            """,
            (
                matricule,
                payload.email,
                payload.nom,
                payload.prenoms,
                payload.telephone,
                payload.commission_id,
                payload.groupe,
                payload.genre,
                payload.date_naissance,
                payload.pays,
                payload.ville,
                payload.intendance_id,
                payload.berger_referent_id,
                payload.date_entree,
                payload.cheminement_pastoral,
            ),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email or matricule already in use",
        ) from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return _read_membre(str(created["id"]), user.role)


@router.get("/membres", response_model=list[MembreProfile])
def list_membres(
    user: Annotated[UserMe, Depends(require_staff)],
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembreProfile]:
    params: list[object] = []
    where = ""
    if q:
        where = (
            "WHERE m.nom ILIKE %s OR m.prenoms ILIKE %s "
            "OR m.matricule ILIKE %s OR m.email ILIKE %s"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    params.extend([limit, offset])
    rows = db.fetch_all(
        f"""
        SELECT {MEMBRE_PROFILE_SELECT}
        {MEMBRE_PROFILE_FROM}
        {where}
        ORDER BY m.matricule ASC
        LIMIT %s OFFSET %s
        """,
        tuple(params),
        role=user.role,
    )
    return [membre_row_to_profile(r) for r in rows]


@router.get("/membres/{membre_id}", response_model=MembreProfile)
def get_membre(
    membre_id: str,
    user: Annotated[UserMe, Depends(require_staff)],
) -> MembreProfile:
    return _read_membre(membre_id, user.role)


@router.patch("/membres/{membre_id}", response_model=MembreProfile)
def update_membre(
    membre_id: str,
    payload: UpdateMembre,
    user: Annotated[UserMe, Depends(require_membre_writer)],
) -> MembreProfile:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return _read_membre(membre_id, user.role)
    columns = ", ".join(f"{name} = %s" for name in fields)
    params = [*fields.values(), membre_id]
    try:
        updated = db.execute(
            f"UPDATE membre SET {columns} WHERE id = %s RETURNING id",
            tuple(params),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email or matricule already in use",
        ) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return _read_membre(membre_id, user.role)


@router.get("/commissions", response_model=list[CommissionOut])
def list_commissions(user: Annotated[UserMe, Depends(require_staff)]) -> list[CommissionOut]:
    rows = db.fetch_all(
        "SELECT id, nom, description FROM commission ORDER BY nom ASC",
        (),
        role=user.role,
    )
    return [CommissionOut(id=str(r["id"]), nom=r["nom"], description=r["description"]) for r in rows]


@router.post("/commissions", response_model=CommissionOut, status_code=status.HTTP_201_CREATED)
def create_commission(
    payload: CreateCommission,
    user: Annotated[UserMe, Depends(require_membre_writer)],
) -> CommissionOut:
    created = db.execute(
        "INSERT INTO commission (nom, description) VALUES (%s, %s) RETURNING id, nom, description",
        (payload.nom, payload.description),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return CommissionOut(
        id=str(created["id"]),
        nom=created["nom"],
        description=created["description"],
    )


@router.get("/evenements", response_model=list[EvenementOut])
def list_evenements(user: Annotated[UserMe, Depends(require_staff)]) -> list[EvenementOut]:
    rows = db.fetch_all(
        """
        SELECT id, titre, type, volet, debut, fin, lieu, session_ouverte
        FROM evenement
        ORDER BY debut DESC
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


@router.post("/evenements", response_model=EvenementOut, status_code=status.HTTP_201_CREATED)
def create_evenement(
    payload: CreateEvenement,
    user: Annotated[UserMe, Depends(require_event_writer)],
) -> EvenementOut:
    created = db.execute(
        """
        INSERT INTO evenement (titre, type, volet, debut, fin, lieu)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, titre, type, volet, debut, fin, lieu, session_ouverte
        """,
        (payload.titre, payload.type, payload.volet, payload.debut, payload.fin, payload.lieu),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return EvenementOut(
        id=str(created["id"]),
        titre=created["titre"],
        type=created["type"],
        volet=created["volet"],
        debut=created["debut"],
        fin=created["fin"],
        lieu=created["lieu"],
        session_ouverte=created["session_ouverte"],
    )
