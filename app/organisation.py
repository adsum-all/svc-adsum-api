"""Organization management endpoints: coordinations, intendances, sous-commissions.

Gives the administration the freedom to build the community structure. Reads are
open to staff; writes are reserved to super_admin and admin, under the per-role
RLS policies (ADR-0002).
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, db
from .deps import require_roles
from .schemas import (
    BergerOut,
    CoordinationOut,
    CreateCoordination,
    CreateIntendance,
    CreateSousCommission,
    IntendanceOut,
    SetPatriarche,
    SousCommissionOut,
    TribuOut,
    UserMe,
)

router = APIRouter(prefix="/api/v1/admin", tags=["organisation"])

STAFF = ("super_admin", "admin", "gestionnaire", "controleur", "direction")
WRITERS = ("super_admin", "admin")
require_staff = require_roles(*STAFF)
require_writer = require_roles(*WRITERS)


def _parent_id(table: str, parent_id: str | None, role: str) -> str | None:
    """Validate an optional parent of the same kind. Returns the id or None; a
    parent is never required, so ``None`` is always valid."""
    if not parent_id:
        return None
    row = db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (parent_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown parent structure")
    return parent_id


@router.get("/coordinations", response_model=list[CoordinationOut])
def list_coordinations(user: Annotated[UserMe, Depends(require_staff)]) -> list[CoordinationOut]:
    rows = db.fetch_all(
        "SELECT c.id, c.nom, c.description, c.publie, c.parent_id, p.nom AS parent "
        "FROM coordination c LEFT JOIN coordination p ON p.id = c.parent_id ORDER BY c.nom ASC",
        (),
        role=user.role,
    )
    return [
        CoordinationOut(
            id=str(r["id"]), nom=r["nom"], description=r["description"], publie=bool(r["publie"]),
            parent_id=str(r["parent_id"]) if r.get("parent_id") else None, parent=r.get("parent"),
        )
        for r in rows
    ]


@router.post("/coordinations", response_model=CoordinationOut, status_code=status.HTTP_201_CREATED)
def create_coordination(
    payload: CreateCoordination,
    user: Annotated[UserMe, Depends(require_writer)],
) -> CoordinationOut:
    parent = _parent_id("coordination", payload.parent_id, user.role)
    created = db.execute(
        "INSERT INTO coordination (nom, description, parent_id) VALUES (%s, %s, %s) "
        "RETURNING id, nom, description, parent_id",
        (payload.nom, payload.description, parent),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return CoordinationOut(
        id=str(created["id"]), nom=created["nom"], description=created["description"],
        parent_id=str(created["parent_id"]) if created.get("parent_id") else None,
    )


@router.get("/intendances", response_model=list[IntendanceOut])
def list_intendances(user: Annotated[UserMe, Depends(require_staff)]) -> list[IntendanceOut]:
    rows = db.fetch_all(
        """
        SELECT i.id, i.nom, i.pays, i.ville, i.coordination_id, i.publie, i.parent_id,
               co.nom AS coordination, p.nom AS parent
        FROM intendance i
        LEFT JOIN coordination co ON co.id = i.coordination_id
        LEFT JOIN intendance p ON p.id = i.parent_id
        ORDER BY i.nom ASC
        """,
        (),
        role=user.role,
    )
    return [
        IntendanceOut(
            id=str(r["id"]),
            nom=r["nom"],
            pays=r["pays"],
            ville=r["ville"],
            coordination_id=str(r["coordination_id"]) if r["coordination_id"] else None,
            coordination=r["coordination"],
            publie=bool(r["publie"]),
            parent_id=str(r["parent_id"]) if r.get("parent_id") else None,
            parent=r.get("parent"),
        )
        for r in rows
    ]


@router.post("/intendances", response_model=IntendanceOut, status_code=status.HTTP_201_CREATED)
def create_intendance(
    payload: CreateIntendance,
    user: Annotated[UserMe, Depends(require_writer)],
) -> IntendanceOut:
    # Coordination and parent intendance are both optional and independent: an
    # intendance never requires either to exist.
    coordination_id = _parent_id("coordination", payload.coordination_id, user.role)
    parent = _parent_id("intendance", payload.parent_id, user.role)
    created = db.execute(
        """
        INSERT INTO intendance (nom, pays, ville, coordination_id, parent_id)
        VALUES (%s, %s, %s, %s, %s) RETURNING id, nom, pays, ville, coordination_id, parent_id
        """,
        (payload.nom, payload.pays, payload.ville, coordination_id, parent),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return IntendanceOut(
        id=str(created["id"]),
        nom=created["nom"],
        pays=created["pays"],
        ville=created["ville"],
        coordination_id=str(created["coordination_id"]) if created["coordination_id"] else None,
        coordination=None,
        parent_id=str(created["parent_id"]) if created.get("parent_id") else None,
    )


@router.get("/sous-commissions", response_model=list[SousCommissionOut])
def list_sous_commissions(user: Annotated[UserMe, Depends(require_staff)]) -> list[SousCommissionOut]:
    rows = db.fetch_all(
        """
        SELECT s.id, s.nom, s.commission_id, s.publie, c.nom AS commission
        FROM sous_commission s
        LEFT JOIN commission c ON c.id = s.commission_id
        ORDER BY s.nom ASC
        """,
        (),
        role=user.role,
    )
    return [
        SousCommissionOut(
            id=str(r["id"]),
            nom=r["nom"],
            commission_id=str(r["commission_id"]) if r["commission_id"] else None,
            commission=r["commission"],
            publie=bool(r["publie"]),
        )
        for r in rows
    ]


@router.post("/sous-commissions", response_model=SousCommissionOut, status_code=status.HTTP_201_CREATED)
def create_sous_commission(
    payload: CreateSousCommission,
    user: Annotated[UserMe, Depends(require_writer)],
) -> SousCommissionOut:
    created = db.execute(
        """
        INSERT INTO sous_commission (nom, commission_id)
        VALUES (%s, %s) RETURNING id, nom, commission_id
        """,
        (payload.nom, payload.commission_id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    return SousCommissionOut(
        id=str(created["id"]),
        nom=created["nom"],
        commission_id=str(created["commission_id"]) if created["commission_id"] else None,
        commission=None,
    )


@router.get("/tribus", response_model=list[TribuOut])
def list_tribus(user: Annotated[UserMe, Depends(require_staff)]) -> list[TribuOut]:
    rows = db.fetch_all(
        "SELECT t.id, t.nom, t.patriarche, t.patriarche_membre_id, "
        "COALESCE(NULLIF(pm.nom_affiche, ''), TRIM(COALESCE(pm.prenoms, '') || ' ' || COALESCE(pm.nom, ''))) AS patriarche_nom "
        "FROM tribu t LEFT JOIN membre pm ON pm.id = t.patriarche_membre_id ORDER BY t.nom ASC",
        (),
        role=user.role,
    )
    return [
        TribuOut(
            id=str(r["id"]), nom=r["nom"], patriarche=r["patriarche"],
            patriarche_membre_id=str(r["patriarche_membre_id"]) if r.get("patriarche_membre_id") else None,
            patriarche_nom=(r.get("patriarche_nom") or None),
        )
        for r in rows
    ]


@router.put("/tribus/{tribu_id}/patriarche")
def set_patriarche(
    tribu_id: str, payload: SetPatriarche, user: Annotated[UserMe, Depends(require_writer)]
) -> dict[str, object]:
    """Assign or revoke the human patriarche of a tribe.

    A tribe has at most one active patriarche (a single column), and the person
    must belong to that tribe. Every appointment and revocation is written to the
    history table and audited. Assigning a new titulaire closes the previous one.
    """
    membre_id = payload.membre_id
    if membre_id is not None:
        membre = db.fetch_one("SELECT tribu_id FROM membre WHERE id = %s", (membre_id,), role=user.role)
        if not membre:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
        if str(membre.get("tribu_id") or "") != str(tribu_id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="le patriarche doit appartenir a cette tribu")
    # Atomic critical section: lock the tribe row so two concurrent appointments
    # cannot both run "close then insert", close the currently open history line,
    # set (or clear) the titulaire and open a new history line, all in one
    # transaction committed once. A partial unique index also forbids two open
    # lines per tribe at the database level (migration 0063).
    with db.connection(user.role) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM tribu WHERE id = %s FOR UPDATE", (tribu_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tribe not found")
        cur.execute("UPDATE tribu_patriarche_historique SET fin = now() WHERE tribu_id = %s AND fin IS NULL", (tribu_id,))
        cur.execute("UPDATE tribu SET patriarche_membre_id = %s WHERE id = %s", (membre_id, tribu_id))
        if membre_id is not None:
            cur.execute(
                "INSERT INTO tribu_patriarche_historique (tribu_id, membre_id, attribue_par, motif) VALUES (%s, %s, %s, %s)",
                (tribu_id, membre_id, user.id, payload.motif),
            )
    audit.log(user.id, user.role, "attribution_patriarche" if membre_id else "revocation_patriarche",
              "tribu", tribu_id, {"membre_id": membre_id, "motif": payload.motif})
    return {"ok": True}


@router.get("/bergers", response_model=list[BergerOut])
def list_bergers(user: Annotated[UserMe, Depends(require_staff)]) -> list[BergerOut]:
    """Users that can be set as a member shepherd, with their member display name."""
    rows = db.fetch_all(
        """
        SELECT u.id, u.role, m.nom, m.prenoms
        FROM utilisateur u
        LEFT JOIN membre m ON m.id = u.membre_id
        WHERE u.actif = true
        ORDER BY m.nom ASC NULLS LAST
        """,
        (),
        role=user.role,
    )
    out: list[BergerOut] = []
    for r in rows:
        name = f"{r['prenoms'] or ''} {r['nom'] or ''}".strip() or r["role"]
        out.append(BergerOut(id=str(r["id"]), nom=name, role=r["role"]))
    return out
