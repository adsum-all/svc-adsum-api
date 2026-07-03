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
    rows = db.fetch_all("SELECT id, nom, patriarche FROM tribu ORDER BY nom ASC", (), role=user.role)
    return [{"id": str(r["id"]), "nom": r["nom"], "patriarche": r["patriarche"]} for r in rows]


@router.get("/commissions")
def commissions(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM commission WHERE publie ORDER BY nom ASC", user.role)


@router.get("/intendances")
def intendances(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM intendance WHERE publie ORDER BY nom ASC", user.role)


@router.get("/coordinations")
def coordinations(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM coordination WHERE publie ORDER BY nom ASC", user.role)


@router.get("/groupes")
def groupes(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, str]]:
    return _list("SELECT id, nom FROM sous_commission WHERE publie ORDER BY nom ASC", user.role)
