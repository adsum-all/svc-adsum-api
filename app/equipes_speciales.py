"""Special teams management: cross-cutting official teams and their membership.

A special team (equipe_speciale) is an official group that is NOT part of the
geographic/functional hierarchy (commission, intendance, tribu): the leadership team,
the shepherds' college, the founder's assistants group, and any other the
administration declares over time. A member belongs to zero or more teams.

Reads and writes are reserved to the ``organisation.equipes`` permission (held by
super_admin and admin, delegable through a governance group), under the per-role RLS
policies (migration 0168, ADR-0002).
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/equipes-speciales", tags=["equipes-speciales"])

_PERM = "organisation.equipes"


class EquipeIn(BaseModel):
    nom: str = Field(min_length=1, max_length=120)
    description: str | None = None
    officielle: bool = True
    ordre: int = 0


class EquipeUpdate(BaseModel):
    nom: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    officielle: bool | None = None
    active: bool | None = None
    ordre: int | None = None


class MembreEquipeIn(BaseModel):
    membre_id: str
    role: str = Field(default="membre", pattern="^(membre|responsable)$")


def _equipe_dict(r: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(r["id"]),
        "code": r.get("code"),
        "nom": r.get("nom"),
        "description": r.get("description") or "",
        "officielle": bool(r.get("officielle")),
        "active": bool(r.get("active")),
        "ordre": int(r.get("ordre") or 0),
        "effectif": int(r.get("effectif") or 0),
    }


_SELECT = (
    "SELECT e.id, e.code, e.nom, e.description, e.officielle, e.active, e.ordre, "
    "(SELECT count(*) FROM membre_equipe_speciale m WHERE m.equipe_id = e.id AND m.actif = true AND m.fin IS NULL) AS effectif "
    "FROM equipe_speciale e "
)


@router.get("")
def list_equipes(user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> list[dict[str, object]]:
    rows = db.fetch_all(_SELECT + "ORDER BY e.ordre ASC, e.nom ASC", (), role=user.role)
    return [_equipe_dict(r) for r in rows]


def _by_id(item_id: str, role: str) -> dict[str, object]:
    row = db.fetch_one(_SELECT + "WHERE e.id = %s", (item_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="équipe introuvable")
    return _equipe_dict(row)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_equipe(payload: EquipeIn, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> dict[str, object]:
    nom = payload.nom.strip()
    if db.fetch_one("SELECT 1 FROM equipe_speciale WHERE lower(nom) = lower(%s)", (nom,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une équipe porte déjà ce nom")
    try:
        created = db.execute(
            "INSERT INTO equipe_speciale (nom, description, officielle, ordre) VALUES (%s, %s, %s, %s) RETURNING id",
            (nom, (payload.description or "").strip() or None, payload.officielle, payload.ordre),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une équipe porte déjà ce nom") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="création impossible")
    audit.log(user.id, user.role, "creation_equipe_speciale", "equipe_speciale", str(created["id"]), {"nom": nom})
    return _by_id(str(created["id"]), user.role)


@router.put("/{item_id}")
def update_equipe(item_id: str, payload: EquipeUpdate, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> dict[str, object]:
    fields = payload.model_dump(exclude_unset=True)
    if "nom" in fields and fields["nom"]:
        nom = str(fields["nom"]).strip()
        dup = db.fetch_one("SELECT 1 FROM equipe_speciale WHERE lower(nom) = lower(%s) AND id <> %s", (nom, item_id), role=user.role)
        if dup:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="une équipe porte déjà ce nom")
        fields["nom"] = nom
    if "description" in fields:
        fields["description"] = (str(fields["description"] or "").strip() or None)
    if not fields:
        return _by_id(item_id, user.role)
    columns = ", ".join(f"{name} = %s" for name in fields)
    updated = db.execute(
        f"UPDATE equipe_speciale SET {columns}, maj_le = now() WHERE id = %s RETURNING id",
        (*fields.values(), item_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="équipe introuvable")
    audit.log(user.id, user.role, "modification_equipe_speciale", "equipe_speciale", item_id, {"champs": list(fields)})
    return _by_id(item_id, user.role)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_equipe(item_id: str, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> None:
    """Delete a team. Refused (409) while it still has active members, so a team is
    never emptied by accident: the administrator removes the members first, or
    deactivates the team instead."""
    exists = db.fetch_one("SELECT code FROM equipe_speciale WHERE id = %s", (item_id,), role=user.role)
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="équipe introuvable")
    count = db.fetch_one("SELECT count(*) AS n FROM membre_equipe_speciale WHERE equipe_id = %s AND actif = true AND fin IS NULL", (item_id,), role=user.role)
    if count and int(count["n"]) > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette équipe a encore des membres : retirez-les ou désactivez l'équipe")
    db.execute("DELETE FROM equipe_speciale WHERE id = %s", (item_id,), role=user.role)
    audit.log(user.id, user.role, "suppression_equipe_speciale", "equipe_speciale", item_id, {})


@router.get("/{item_id}/membres")
def list_membres(item_id: str, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> list[dict[str, object]]:
    _by_id(item_id, user.role)  # 404 if the team is unknown
    rows = db.fetch_all(
        "SELECT mes.id, mes.membre_id, mes.role, m.matricule, "
        "TRIM(COALESCE(m.prenoms, '') || ' ' || COALESCE(m.nom, '')) AS nom "
        "FROM membre_equipe_speciale mes JOIN membre m ON m.id = mes.membre_id "
        "WHERE mes.equipe_id = %s AND mes.actif = true AND mes.fin IS NULL ORDER BY m.nom ASC",
        (item_id,),
        role=user.role,
    )
    return [
        {"id": str(r["id"]), "membre_id": str(r["membre_id"]), "role": r.get("role") or "membre",
         "matricule": r.get("matricule"), "nom": (str(r.get("nom") or "").strip() or None)}
        for r in rows
    ]


@router.post("/{item_id}/membres", status_code=status.HTTP_201_CREATED)
def add_membre(item_id: str, payload: MembreEquipeIn, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> dict[str, object]:
    _by_id(item_id, user.role)
    if not db.fetch_one("SELECT 1 FROM membre WHERE id = %s", (payload.membre_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
    # Idempotent: re-adding an existing membership reactivates it and updates the role.
    # A closed one opens a NEW period rather than pretending the old one never ended,
    # and an already-open one keeps its start date: only the role changes.
    db.execute(
        "INSERT INTO membre_equipe_speciale (equipe_id, membre_id, role, actif, debut, attribue_par) "
        "VALUES (%s, %s, %s, true, now(), %s) "
        "ON CONFLICT (equipe_id, membre_id) DO UPDATE SET "
        "  actif = true, role = EXCLUDED.role, "
        "  debut = CASE WHEN membre_equipe_speciale.actif THEN membre_equipe_speciale.debut ELSE now() END, "
        "  fin = NULL, "
        "  attribue_par = CASE WHEN membre_equipe_speciale.actif "
        "                      THEN membre_equipe_speciale.attribue_par ELSE EXCLUDED.attribue_par END",
        (item_id, payload.membre_id, payload.role, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "ajout_membre_equipe_speciale", "equipe_speciale", item_id, {"membre_id": payload.membre_id, "role": payload.role})
    return {"ok": True}


@router.delete("/{item_id}/membres/{membre_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_membre(item_id: str, membre_id: str, user: Annotated[UserMe, Depends(require_permission(_PERM))]) -> None:
    """End somebody's membership of a special team, keeping the period it covered.

    The row used to be deleted outright, so who sat on a body last year was
    unanswerable: for a governance organ, that is the one question worth recording.
    The membership is closed instead, which is what the debut/fin columns are for.
    """
    removed = db.execute(
        "UPDATE membre_equipe_speciale SET actif = false, fin = now() "
        "WHERE equipe_id = %s AND membre_id = %s AND actif = true AND fin IS NULL "
        "RETURNING id",
        (item_id, membre_id),
        role=user.role,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ce membre ne fait pas partie de l'équipe")
    audit.log(user.id, user.role, "retrait_membre_equipe_speciale", "equipe_speciale", item_id, {"membre_id": membre_id})
