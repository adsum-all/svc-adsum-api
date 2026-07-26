"""Audit logging and the audit journal endpoint.

Every sensitive mutation records an append-only audit entry (who, what action,
on which object, when). The journal is readable by super_admin and admin only.
The audit table is range-partitioned by month (ADR-0001).
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

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


def log_cursor(
    cur: Any,
    acteur_id: str | None,
    acteur_role: str | None,
    action: str,
    objet_type: str | None = None,
    objet_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record an audit entry on an EXISTING cursor, so the audit row is part of the caller's
    transaction (atomic with the mutation). Use this where losing the audit trail would be a
    compliance risk, e.g. a cascade that removes rights: the entry is the only record of what
    was withdrawn. Unlike :func:`log`, a failure here rolls the whole transaction back, which
    is the intended all-or-nothing guarantee (the mutation must not commit without its trace).
    The RLS role is already set on the connection by the caller (db.connection(role=...))."""
    cur.execute(
        "INSERT INTO audit (acteur_id, acteur_role, action, objet_type, objet_id, details) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (
            acteur_id,
            acteur_role,
            action,
            objet_type,
            objet_id,
            json.dumps(details) if details is not None else None,
        ),
    )


@router.get("", response_model=list[AuditEntry])
def list_audit(
    user: Annotated[UserMe, Depends(require_permission("audit.administrer"))],
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=50, ge=1, le=200),
    action: str | None = None,
    objet_type: str | None = None,
    acteur_role: str | None = None,
    depuis: str | None = None,
    jusqua: str | None = None,
    reponse: Response = None,  # type: ignore[assignment]
) -> list[AuditEntry]:
    """The audit journal, paginated and filterable.

    It used to return the last two hundred entries and nothing else, so on this base
    ninety-three percent of the journal was unreachable and the window covered barely
    two days. A page that claims full traceability must actually reach every entry.

    The shape stays a list so existing callers keep working; the counts travel in the
    X-Total-Count and X-Total-Pages headers.
    """
    conditions: list[str] = []
    params: list[object] = []
    for colonne, valeur in (("a.action", action), ("a.objet_type", objet_type), ("a.acteur_role", acteur_role)):
        if valeur:
            conditions.append(f"{colonne} = %s")
            params.append(valeur)
    if depuis:
        conditions.append("a.horodatage >= %s")
        params.append(depuis)
    if jusqua:
        conditions.append("a.horodatage <= %s")
        params.append(jusqua)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    compte = db.fetch_one(f"SELECT count(*) AS n FROM audit a {where}", tuple(params), role=user.role)
    total = int((compte or {}).get("n") or 0)
    if reponse is not None:
        reponse.headers["X-Total-Count"] = str(total)
        reponse.headers["X-Total-Pages"] = str(max(1, (total + taille - 1) // taille))
        reponse.headers["X-Page"] = str(page)
    rows = db.fetch_all(
        f"""
        SELECT a.id, a.acteur_role, a.action, a.objet_type, a.objet_id, a.horodatage,
               trim(coalesce(m.prenoms, '') || ' ' || coalesce(m.nom, '')) AS acteur_nom
        FROM audit a
        LEFT JOIN utilisateur u ON u.id = a.acteur_id
        LEFT JOIN membre m ON m.id = u.membre_id
        {where}
        ORDER BY a.horodatage DESC
        LIMIT %s OFFSET %s
        """,
        tuple([*params, taille, (page - 1) * taille]),
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
