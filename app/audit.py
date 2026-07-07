"""Audit logging and the audit journal endpoint.

Every sensitive mutation records an append-only audit entry (who, what action,
on which object, when). The journal is readable by super_admin and admin only.
The audit table is range-partitioned by month (ADR-0001).
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from . import db
from .permissions_rbac import require_permission
from .schemas import AuditEntry, UserMe

router = APIRouter(prefix="/api/v1/admin/audit", tags=["audit"])



def log(
    acteur_id: str | None,
    acteur_role: str | None,
    action: str,
    objet_type: str | None = None,
    objet_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record an audit entry. Never raises into the caller path."""
    try:
        db.execute(
            """
            INSERT INTO audit (acteur_id, acteur_role, action, objet_type, objet_id, details)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                acteur_id,
                acteur_role,
                action,
                objet_type,
                objet_id,
                json.dumps(details) if details is not None else None,
            ),
            role=acteur_role,
        )
    except Exception:  # noqa: BLE001 - auditing must not break the business action
        pass


@router.get("", response_model=list[AuditEntry])
def list_audit(user: Annotated[UserMe, Depends(require_permission("audit.administrer"))]) -> list[AuditEntry]:
    rows = db.fetch_all(
        """
        SELECT a.id, a.acteur_role, a.action, a.objet_type, a.objet_id, a.horodatage,
               trim(coalesce(m.prenoms, '') || ' ' || coalesce(m.nom, '')) AS acteur_nom
        FROM audit a
        LEFT JOIN utilisateur u ON u.id = a.acteur_id
        LEFT JOIN membre m ON m.id = u.membre_id
        ORDER BY a.horodatage DESC
        LIMIT 200
        """,
        (),
        role=user.role,
    )
    out: list[AuditEntry] = []
    for r in rows:
        name = r.get("acteur_nom")
        out.append(
            AuditEntry(
                id=int(r["id"]),
                acteur_role=r["acteur_role"] if isinstance(r["acteur_role"], str) else None,
                acteur_nom=name if isinstance(name, str) and name else None,
                action=str(r["action"]),
                objet_type=r["objet_type"] if isinstance(r["objet_type"], str) else None,
                objet_id=str(r["objet_id"]) if r.get("objet_id") else None,
                horodatage=r.get("horodatage"),  # type: ignore[arg-type]
            )
        )
    return out
