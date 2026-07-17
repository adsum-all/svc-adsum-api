"""Organization management endpoints: coordinations, intendances, sous-commissions.

Gives the administration the freedom to build the community structure. Reads are
open to staff; writes are reserved to super_admin and admin, under the per-role
RLS policies (ADR-0002).
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .fonctions_membre import label_genre
from .permissions_rbac import require_permission
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
    UpdateCoordination,
    UpdateIntendance,
    UserMe,
)

router = APIRouter(prefix="/api/v1/admin", tags=["organisation"])



def _parent_id(table: str, parent_id: str | None, role: str) -> str | None:
    """Validate an optional parent of the same kind. Returns the id or None; a
    parent is never required, so ``None`` is always valid."""
    if not parent_id:
        return None
    row = db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (parent_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown parent structure")
    return parent_id


def _verifier_cycle(table: str, item_id: str, parent_id: str | None, role: str) -> None:
    """Refuse a parent link that would create a cycle in the self-hierarchy.

    Walks the ancestor chain from ``parent_id`` upward: if ``item_id`` reappears,
    the link would close a cycle (A parent of B, B parent of A, ...). Self-parent
    is already blocked by a DB CHECK; this covers the multi-level case the base
    cannot. ``table`` is a fixed internal value (never user input).
    """
    if not parent_id:
        return
    seen: set[str] = {item_id}
    current: str | None = parent_id
    for _ in range(64):  # depth guard; real hierarchies are shallow
        if current is None:
            return
        if current in seen:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cette relation creerait un cycle")
        seen.add(current)
        row = db.fetch_one(f"SELECT parent_id FROM {table} WHERE id = %s", (current,), role=role)
        current = str(row["parent_id"]) if row and row.get("parent_id") else None


def _nom(prenoms: object, nom: object) -> str | None:
    name = f"{prenoms or ''} {nom or ''}".strip()
    return name or None


# LATERAL fragment resolving a structure's responsable: the member attached to
# that structure (by the given FK column) who holds the given function, mirroring
# how the patriarche is resolved for a tribe. Gender is carried for the title.
def _responsable_lateral(fk_column: str, fonction: str) -> str:
    return (
        "LEFT JOIN LATERAL ("
        "  SELECT rm.prenoms AS r_prenoms, rm.nom AS r_nom, rm.genre AS r_genre FROM membre rm "
        f"  WHERE rm.{fk_column} = s.id AND (rm.fonction_cle = '{fonction}' "
        f"    OR EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = rm.id AND lower(mf.fonction_cle) = '{fonction}' AND mf.actif)) "
        "  ORDER BY rm.nom LIMIT 1"
        ") resp ON true "
        f"LEFT JOIN fonction_honorifique frt ON frt.cle = '{fonction}' "
    )


def _coordination_out(r: dict[str, object]) -> CoordinationOut:
    resp = _nom(r.get("r_prenoms"), r.get("r_nom"))
    return CoordinationOut(
        id=str(r["id"]), nom=str(r["nom"]), description=r.get("description"),  # type: ignore[arg-type]
        pays_code=r.get("pays_code"), pays=r.get("pays"), continent=r.get("continent"), ville=r.get("ville"),  # type: ignore[arg-type]
        statut=str(r.get("statut") or "actif"), publie=bool(r.get("publie")),
        parent_id=str(r["parent_id"]) if r.get("parent_id") else None, parent=r.get("parent"),  # type: ignore[arg-type]
        responsable=resp,
        responsable_titre=(label_genre(r.get("r_genre"), r.get("frt_h"), r.get("frt_f"), r.get("frt_n")) if resp else None),
    )


_COORD_SELECT = (
    "SELECT s.id, s.nom, s.description, s.pays_code, s.pays, s.continent, s.ville, s.statut, s.publie, s.parent_id, p.nom AS parent, "
    "resp.r_prenoms, resp.r_nom, resp.r_genre, frt.libelle_h AS frt_h, frt.libelle_f AS frt_f, frt.libelle_n AS frt_n "
    "FROM coordination s LEFT JOIN coordination p ON p.id = s.parent_id "
)


@router.get("/coordinations", response_model=list[CoordinationOut])
def list_coordinations(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> list[CoordinationOut]:
    rows = db.fetch_all(
        _COORD_SELECT + _responsable_lateral("coordination_id", "coordinateur") + "ORDER BY s.nom ASC",
        (),
        role=user.role,
    )
    return [_coordination_out(r) for r in rows]


def _coordination_by_id(item_id: str, role: str) -> CoordinationOut:
    row = db.fetch_one(
        _COORD_SELECT + _responsable_lateral("coordination_id", "coordinateur") + "WHERE s.id = %s",
        (item_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="coordination not found")
    return _coordination_out(row)


@router.post("/coordinations", response_model=CoordinationOut, status_code=status.HTTP_201_CREATED)
def create_coordination(
    payload: CreateCoordination,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> CoordinationOut:
    parent = _parent_id("coordination", payload.parent_id, user.role)
    created = db.execute(
        "INSERT INTO coordination (nom, description, pays_code, continent, ville, statut, parent_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (payload.nom, payload.description, payload.pays_code, payload.continent, payload.ville, payload.statut, parent),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    audit.log(user.id, user.role, "creation_organisation", "coordination", str(created["id"]), {"nom": payload.nom})
    return _coordination_by_id(str(created["id"]), user.role)


@router.put("/coordinations/{item_id}", response_model=CoordinationOut)
def update_coordination(
    item_id: str,
    payload: UpdateCoordination,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> CoordinationOut:
    """Full edit of a coordination: descriptive/geographic fields and the optional
    parent link, with a cycle guard. Only provided fields are written."""
    fields = payload.model_dump(exclude_unset=True)
    if "parent_id" in fields:
        fields["parent_id"] = _parent_id("coordination", fields.get("parent_id"), user.role)
        _verifier_cycle("coordination", item_id, fields.get("parent_id"), user.role)
    if not fields:
        return _coordination_by_id(item_id, user.role)
    columns = ", ".join(f"{name} = %s" for name in fields)
    updated = db.execute(
        f"UPDATE coordination SET {columns} WHERE id = %s RETURNING id",
        (*fields.values(), item_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="coordination not found")
    audit.log(user.id, user.role, "modification_organisation", "coordination", item_id, {"champs": list(fields)})
    return _coordination_by_id(item_id, user.role)


def _intendance_out(r: dict[str, object]) -> IntendanceOut:
    resp = _nom(r.get("r_prenoms"), r.get("r_nom"))
    return IntendanceOut(
        id=str(r["id"]), nom=str(r["nom"]), description=r.get("description"),  # type: ignore[arg-type]
        pays_code=r.get("pays_code"), pays=r.get("pays"), continent=r.get("continent"), ville=r.get("ville"),  # type: ignore[arg-type]
        statut=str(r.get("statut") or "actif"),
        coordination_id=str(r["coordination_id"]) if r.get("coordination_id") else None,
        coordination=r.get("coordination"),  # type: ignore[arg-type]
        publie=bool(r.get("publie")),
        parent_id=str(r["parent_id"]) if r.get("parent_id") else None, parent=r.get("parent"),  # type: ignore[arg-type]
        responsable=resp,
        responsable_titre=(label_genre(r.get("r_genre"), r.get("frt_h"), r.get("frt_f"), r.get("frt_n")) if resp else None),
    )


_INTENDANCE_SELECT = (
    "SELECT s.id, s.nom, s.description, s.pays_code, s.pays, s.continent, s.ville, s.statut, s.coordination_id, s.publie, s.parent_id, "
    "co.nom AS coordination, p.nom AS parent, "
    "resp.r_prenoms, resp.r_nom, resp.r_genre, frt.libelle_h AS frt_h, frt.libelle_f AS frt_f, frt.libelle_n AS frt_n "
    "FROM intendance s LEFT JOIN coordination co ON co.id = s.coordination_id LEFT JOIN intendance p ON p.id = s.parent_id "
)


def _intendance_by_id(item_id: str, role: str) -> IntendanceOut:
    row = db.fetch_one(
        _INTENDANCE_SELECT + _responsable_lateral("intendance_id", "intendant") + "WHERE s.id = %s",
        (item_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intendance not found")
    return _intendance_out(row)


@router.get("/intendances", response_model=list[IntendanceOut])
def list_intendances(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> list[IntendanceOut]:
    rows = db.fetch_all(
        _INTENDANCE_SELECT + _responsable_lateral("intendance_id", "intendant") + "ORDER BY s.nom ASC",
        (),
        role=user.role,
    )
    return [_intendance_out(r) for r in rows]


@router.post("/intendances", response_model=IntendanceOut, status_code=status.HTTP_201_CREATED)
def create_intendance(
    payload: CreateIntendance,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> IntendanceOut:
    # Coordination and parent intendance are both optional and independent: an
    # intendance never requires either to exist.
    coordination_id = _parent_id("coordination", payload.coordination_id, user.role)
    parent = _parent_id("intendance", payload.parent_id, user.role)
    created = db.execute(
        "INSERT INTO intendance (nom, description, pays_code, pays, continent, ville, statut, coordination_id, parent_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (payload.nom, payload.description, payload.pays_code, payload.pays, payload.continent, payload.ville, payload.statut, coordination_id, parent),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    audit.log(user.id, user.role, "creation_organisation", "intendance", str(created["id"]), {"nom": payload.nom})
    return _intendance_by_id(str(created["id"]), user.role)


@router.put("/intendances/{item_id}", response_model=IntendanceOut)
def update_intendance(
    item_id: str,
    payload: UpdateIntendance,
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> IntendanceOut:
    """Full edit of an intendance: descriptive/geographic fields, the optional
    coordination link and the optional parent intendance, with a cycle guard."""
    fields = payload.model_dump(exclude_unset=True)
    if "coordination_id" in fields:
        fields["coordination_id"] = _parent_id("coordination", fields.get("coordination_id"), user.role)
    if "parent_id" in fields:
        fields["parent_id"] = _parent_id("intendance", fields.get("parent_id"), user.role)
        _verifier_cycle("intendance", item_id, fields.get("parent_id"), user.role)
    if not fields:
        return _intendance_by_id(item_id, user.role)
    columns = ", ".join(f"{name} = %s" for name in fields)
    updated = db.execute(
        f"UPDATE intendance SET {columns} WHERE id = %s RETURNING id",
        (*fields.values(), item_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intendance not found")
    audit.log(user.id, user.role, "modification_organisation", "intendance", item_id, {"champs": list(fields)})
    return _intendance_by_id(item_id, user.role)


@router.get("/sous-commissions", response_model=list[SousCommissionOut])
def list_sous_commissions(user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))]) -> list[SousCommissionOut]:
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
    user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
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
    audit.log(user.id, user.role, "creation_organisation", "sous_commission", str(created["id"]), {"nom": created["nom"]})
    return SousCommissionOut(
        id=str(created["id"]),
        nom=created["nom"],
        commission_id=str(created["commission_id"]) if created["commission_id"] else None,
        commission=None,
    )


@router.get("/tribus", response_model=list[TribuOut])
def list_tribus(user: Annotated[UserMe, Depends(require_permission("tribus.consulter"))]) -> list[TribuOut]:
    rows = db.fetch_all(
        "SELECT t.id, t.nom, t.description, t.publie, t.patriarche, patr.membre_id AS patriarche_membre_id, "
        "TRIM(COALESCE(patr.prenoms, '') || ' ' || COALESCE(patr.nom, '')) AS patriarche_nom "
        "FROM tribu t LEFT JOIN LATERAL ("
        "  SELECT pm2.id AS membre_id, pm2.prenoms, pm2.nom FROM membre pm2 "
        "  WHERE pm2.tribu_id = t.id AND (pm2.fonction_cle = 'patriarche' "
        "    OR EXISTS (SELECT 1 FROM membre_fonction mf WHERE mf.membre_id = pm2.id AND lower(mf.fonction_cle) = 'patriarche' AND mf.actif)) "
        "  ORDER BY pm2.nom LIMIT 1"
        ") patr ON true ORDER BY t.nom ASC",
        (),
        role=user.role,
    )
    return [
        TribuOut(
            id=str(r["id"]), nom=r["nom"], description=r.get("description"), publie=bool(r.get("publie")),
            patriarche=r["patriarche"],
            patriarche_membre_id=str(r["patriarche_membre_id"]) if r.get("patriarche_membre_id") else None,
            patriarche_nom=((r.get("patriarche_nom") or "").strip() or None),
        )
        for r in rows
    ]


class TribuIn(BaseModel):
    nom: str = Field(min_length=1, max_length=120)
    description: str | None = None


@router.post("/tribus", response_model=TribuOut, status_code=status.HTTP_201_CREATED)
def create_tribu(
    payload: TribuIn, user: Annotated[UserMe, Depends(require_permission("tribus.administrer"))]
) -> TribuOut:
    """Create a tribe (dynamic referential: the list is never hardcoded).

    The tribe name is the unique code: a case-insensitive duplicate is rejected with
    a clear 409 instead of surfacing a raw unique-constraint violation as a 500."""
    nom = payload.nom.strip()
    if db.fetch_one("SELECT 1 FROM tribu WHERE lower(nom) = lower(%s)", (nom,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une tribu porte deja ce nom")
    try:
        created = db.execute(
            "INSERT INTO tribu (nom, description) VALUES (%s, %s) RETURNING id, nom, description, publie",
            (nom, (payload.description or "").strip() or None),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une tribu porte deja ce nom") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="tribe not created")
    audit.log(user.id, user.role, "creation_tribu", "tribu", str(created["id"]), {"nom": payload.nom})
    return TribuOut(
        id=str(created["id"]), nom=created["nom"], description=created.get("description"),
        publie=bool(created.get("publie")), patriarche=None, patriarche_membre_id=None, patriarche_nom=None,
    )


@router.put("/tribus/{tribu_id}/patriarche")
def set_patriarche(
    tribu_id: str, payload: SetPatriarche, user: Annotated[UserMe, Depends(require_permission("tribus.administrer"))]
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
def list_bergers(user: Annotated[UserMe, Depends(require_permission("bergers.consulter"))]) -> list[BergerOut]:
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
