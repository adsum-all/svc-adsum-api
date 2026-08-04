"""Member-facing reference lists for registration dropdowns.

Any authenticated user can read these; only PUBLISHED organizational entries are
returned so unpublished admin drafts never appear in the member dropdowns. The
twelve tribes are always available (read-only, seeded). Values come from the
database, never hardcoded on the client.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import db
from .auth import current_user
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/reference", tags=["reference"])


def _list(sql: str, role: str) -> list[dict[str, str]]:
    rows = db.fetch_all(sql, (), role=role)
    return [{"id": str(r["id"]), "nom": r["nom"]} for r in rows]


@router.get("/tribus")
def tribus(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str | None]]:
    """The tribes, with the colour each one is known by.

    The colour travels with the name because that is how a member finds their own:
    they recognise it before they read anything. It is null when the organisation has
    not chosen one, and the interface then shows no swatch rather than inventing a
    colour that would come to stand for a group nobody assigned it to.
    """
    rows = db.fetch_all("SELECT id, nom, patriarche, couleur FROM tribu ORDER BY nom ASC", (), role=user.role)
    return [
        {"id": str(r["id"]), "nom": r["nom"], "patriarche": r["patriarche"], "couleur": r.get("couleur")}
        for r in rows
    ]


@router.get("/commissions")
def commissions(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    # Includes the unit type so the member registration dropdown can prefix
    # "Commission" or "Mission", exactly as the back office does.
    rows = db.fetch_all(
        "SELECT id, nom, type_organisation FROM commission WHERE publie ORDER BY type_organisation, nom ASC",
        (),
        role=user.role,
    )
    return [
        {"id": str(r["id"]), "nom": r["nom"], "type_organisation": r.get("type_organisation") or "commission"}
        for r in rows
    ]


@router.get("/intendances")
def intendances(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM intendance WHERE publie ORDER BY nom ASC", user.role)


@router.get("/coordinations")
def coordinations(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM coordination WHERE publie ORDER BY nom ASC", user.role)


@router.get("/groupes")
def groupes(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM sous_commission WHERE publie ORDER BY nom ASC", user.role)


@router.get("/types-evenements")
def types_evenements(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str | None]]:
    # Published event types with their unique colour, for the planning dropdowns
    # (back office and collaboration) and for colouring the member calendar.
    rows = db.fetch_all(
        "SELECT id, code, nom, couleur, description FROM type_evenement WHERE publie ORDER BY ordre, nom ASC",
        (),
        role=user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "code": r["code"],
            "nom": r["nom"],
            "couleur": r["couleur"],
            "description": r.get("description"),
        }
        for r in rows
    ]
