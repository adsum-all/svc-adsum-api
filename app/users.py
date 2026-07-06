"""User and rights management: list, create and update application accounts.

Following least privilege, only super_admin and admin manage accounts. The first
super admin creates the others. Passwords are hashed with Argon2; the plain value
never leaves the request.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import (
    BulkCreateUtilisateurs,
    BulkResult,
    CreateUtilisateur,
    UpdateUtilisateur,
    UserMe,
    UtilisateurOut,
)
from .security import hash_password

router = APIRouter(prefix="/api/v1/admin/utilisateurs", tags=["utilisateurs"])


_SELECT = (
    "SELECT u.id, u.email, u.role, u.actif, u.double_facteur, u.membre_id, u.dernier_login, "
    "trim(coalesce(m.prenoms, '') || ' ' || coalesce(m.nom, '')) AS membre_nom "
    "FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id"
)


def _to_out(row: dict[str, object]) -> UtilisateurOut:
    name = row.get("membre_nom")
    return UtilisateurOut(
        id=str(row["id"]),
        email=str(row["email"]),
        role=str(row["role"]),
        actif=bool(row["actif"]),
        double_facteur=bool(row["double_facteur"]),
        membre_id=str(row["membre_id"]) if row.get("membre_id") else None,
        membre_nom=name if isinstance(name, str) and name else None,
        dernier_login=row.get("dernier_login"),  # type: ignore[arg-type]
    )


@router.get("", response_model=list[UtilisateurOut])
def list_utilisateurs(
    user: Annotated[UserMe, Depends(require_permission("comptes.administrer"))]
) -> list[UtilisateurOut]:
    rows = db.fetch_all(f"{_SELECT} ORDER BY u.role ASC, u.email ASC", (), role=user.role)
    return [_to_out(r) for r in rows]


@router.post("", response_model=UtilisateurOut, status_code=status.HTTP_201_CREATED)
def create_utilisateur(
    payload: CreateUtilisateur,
    user: Annotated[UserMe, Depends(require_permission("comptes.systeme"))],
) -> UtilisateurOut:
    try:
        # An account is always created as 'membre'. Elevated access is granted
        # afterwards only through an access group, never at account creation.
        created = db.execute(
            """
            INSERT INTO utilisateur (email, hash_mdp, role, membre_id, double_facteur, actif)
            VALUES (%s, %s, 'membre', %s, %s, true)
            RETURNING id
            """,
            (payload.email, hash_password(payload.password), payload.membre_id,
             payload.double_facteur),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already in use") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    new_id = str(created["id"])
    row = db.fetch_one(f"{_SELECT} WHERE u.id = %s", (new_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="read failed")
    audit.log(user.id, user.role, "creation_compte", "utilisateur", new_id, {"role": "membre"})
    return _to_out(row)


@router.post("/lot", response_model=BulkResult)
def bulk_create(
    payload: BulkCreateUtilisateurs,
    user: Annotated[UserMe, Depends(require_permission("comptes.systeme"))],
) -> BulkResult:
    """Create access accounts in bulk; profiles are completed later by each member."""
    crees = 0
    doublons: list[str] = []
    erreurs: list[dict[str, str]] = []
    for compte in payload.comptes:
        try:
            created = db.execute(
                """
                INSERT INTO utilisateur (email, hash_mdp, role, actif)
                VALUES (%s, %s, 'membre', true)
                ON CONFLICT (email) DO NOTHING
                RETURNING id
                """,
                (compte.email, hash_password(compte.password)),
                role=user.role,
            )
            if created:
                crees += 1
            else:
                doublons.append(str(compte.email))
        except Exception as exc:  # noqa: BLE001 - report per-row, keep going
            erreurs.append({"email": str(compte.email), "raison": type(exc).__name__})
    audit.log(user.id, user.role, "creation_comptes_masse", "utilisateur", None, {"crees": crees})
    return BulkResult(crees=crees, doublons=doublons, erreurs=erreurs)


@router.patch("/{utilisateur_id}", response_model=UtilisateurOut)
def update_utilisateur(
    utilisateur_id: str,
    payload: UpdateUtilisateur,
    user: Annotated[UserMe, Depends(require_permission("comptes.systeme"))],
) -> UtilisateurOut:
    fields = payload.model_dump(exclude_unset=True)
    if fields:
        # Capture the prior values so the audit trail records the actual change
        # (role/actif before and after), not merely which fields were touched.
        # UpdateUtilisateur carries no secret, so the values are safe to log.
        before = db.fetch_one(
            "SELECT role, actif, double_facteur FROM utilisateur WHERE id = %s",
            (utilisateur_id,),
            role=user.role,
        )
        columns = ", ".join(f"{name} = %s" for name in fields)
        updated = db.execute(
            f"UPDATE utilisateur SET {columns} WHERE id = %s RETURNING id",
            (*fields.values(), utilisateur_id),
            role=user.role,
        )
        if not updated:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
        avant = {name: (before.get(name) if before else None) for name in fields}
        audit.log(
            user.id,
            user.role,
            "modification_compte",
            "utilisateur",
            utilisateur_id,
            {"champs": list(fields), "avant": avant, "apres": fields},
        )
    row = db.fetch_one(f"{_SELECT} WHERE u.id = %s", (utilisateur_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    return _to_out(row)
