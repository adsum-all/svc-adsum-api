"""Scan terminal management: register, list, authorize and revoke terminals.

Only registered and authorized terminals may check members in. The private key
never lives on a terminal; a lost device can be revoked remotely (autorise=false).
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import CreateTerminal, TerminalOut, UpdateTerminal, UserMe

router = APIRouter(prefix="/api/v1/admin/terminaux", tags=["terminaux"])


_SELECT = "SELECT id, nom, appareil_id, autorise, appaire_le, dernier_sync FROM terminal"


def _to_out(row: dict[str, object]) -> TerminalOut:
    return TerminalOut(
        id=str(row["id"]),
        nom=row["nom"] if isinstance(row["nom"], str) else None,
        appareil_id=row["appareil_id"] if isinstance(row["appareil_id"], str) else None,
        autorise=bool(row["autorise"]),
        appaire_le=row.get("appaire_le"),  # type: ignore[arg-type]
        dernier_sync=row.get("dernier_sync"),  # type: ignore[arg-type]
    )


@router.get("", response_model=list[TerminalOut])
def list_terminaux(user: Annotated[UserMe, Depends(require_permission("terminaux.consulter"))]) -> list[TerminalOut]:
    rows = db.fetch_all(f"{_SELECT} ORDER BY nom ASC NULLS LAST", (), role=user.role)
    return [_to_out(r) for r in rows]


@router.post("", response_model=TerminalOut, status_code=status.HTTP_201_CREATED)
def register_terminal(
    payload: CreateTerminal,
    user: Annotated[UserMe, Depends(require_permission("terminaux.administrer"))],
) -> TerminalOut:
    try:
        created = db.execute(
            """
            INSERT INTO terminal (nom, appareil_id, autorise, appaire_le)
            VALUES (%s, %s, true, now())
            RETURNING id
            """,
            (payload.nom, payload.appareil_id),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="appareil_id already registered") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    new_id = str(created["id"])
    row = db.fetch_one(f"{_SELECT} WHERE id = %s", (new_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="read failed")
    audit.log(user.id, user.role, "enregistrement_terminal", "terminal", new_id, {"nom": payload.nom})
    return _to_out(row)


@router.patch("/{terminal_id}", response_model=TerminalOut)
def update_terminal(
    terminal_id: str,
    payload: UpdateTerminal,
    user: Annotated[UserMe, Depends(require_permission("terminaux.administrer"))],
) -> TerminalOut:
    fields = payload.model_dump(exclude_unset=True)
    if fields:
        columns = ", ".join(f"{name} = %s" for name in fields)
        updated = db.execute(
            f"UPDATE terminal SET {columns} WHERE id = %s RETURNING id",
            (*fields.values(), terminal_id),
            role=user.role,
        )
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="terminal not found")
        if "autorise" in fields:
            act = "autorisation_terminal" if fields["autorise"] else "revocation_terminal"
            audit.log(user.id, user.role, act, "terminal", terminal_id)
    row = db.fetch_one(f"{_SELECT} WHERE id = %s", (terminal_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="terminal not found")
    return _to_out(row)
