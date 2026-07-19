# ruff: noqa: E501 - long SQL statements
"""Interim / suppleance: a dated, audited temporary stand-in covering an unavailable
titulaire on a function and perimeter. While active, the member hierarchy shows the
suppleant as "par interim" at the covered level; when it ends the titulaire is restored
automatically (the ledger is never queried once terminated).
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/organigramme", tags=["organigramme-interim"])

_ACTIF = "i.statut = 'actif' AND i.date_debut <= current_date AND (i.date_fin IS NULL OR i.date_fin >= current_date)"


def _nom(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    aff = row.get("nom_affiche")
    if aff:
        return str(aff)
    nom = f"{row.get('prenoms') or ''} {row.get('nom') or ''}".strip()
    return nom or None


def interims_actifs(role: str | None) -> list[dict[str, Any]]:
    """Currently-active interims, with resolved names. Used by the member hierarchy to
    show a covered level as "par interim"."""
    rows = db.fetch_all(
        "SELECT i.id, i.fonction_cle, i.perimetre, i.motif, i.date_debut, i.date_fin, "
        "s.prenoms AS s_p, s.nom AS s_n, s.nom_affiche AS s_a, "
        "t.prenoms AS t_p, t.nom AS t_n, t.nom_affiche AS t_a "
        "FROM organisation_interim i JOIN membre s ON s.id = i.suppleant_id "
        "LEFT JOIN membre t ON t.id = i.titulaire_id "
        f"WHERE {_ACTIF}",
        (), role=role,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": str(r["id"]),
            "fonction_cle": str(r["fonction_cle"]).lower(),
            "perimetre": r.get("perimetre") or None,
            "suppleant": _nom({"prenoms": r.get("s_p"), "nom": r.get("s_n"), "nom_affiche": r.get("s_a")}),
            "titulaire": _nom({"prenoms": r.get("t_p"), "nom": r.get("t_n"), "nom_affiche": r.get("t_a")}),
            "motif": r.get("motif") or None,
            "date_fin": r["date_fin"].isoformat() if r.get("date_fin") else None,
        })
    return out


def _out(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "fonction_cle": r.get("fonction_cle"), "perimetre": r.get("perimetre") or None,
        "titulaire_id": str(r["titulaire_id"]) if r.get("titulaire_id") else None,
        "suppleant_id": str(r["suppleant_id"]) if r.get("suppleant_id") else None,
        "motif": r.get("motif") or None,
        "date_debut": r["date_debut"].isoformat() if r.get("date_debut") else None,
        "date_fin": r["date_fin"].isoformat() if r.get("date_fin") else None,
        "statut": r.get("statut"),
    }


class InterimIn(BaseModel):
    fonction_cle: str = Field(min_length=1, max_length=40)
    perimetre: str | None = Field(default=None, max_length=160)
    titulaire_id: UUID | None = None
    suppleant_id: UUID
    # Free text; must NOT carry health or personal-sensitive data (RGPD minimisation):
    # it is visible to holders of organisation.consulter.
    motif: str | None = Field(default=None, max_length=300)
    date_debut: date | None = None
    date_fin: date | None = None

    @model_validator(mode="after")
    def _dates_coherentes(self) -> InterimIn:
        if self.date_fin and self.date_debut and self.date_fin < self.date_debut:
            raise ValueError("date_fin antérieure à date_debut")
        return self


@router.get("/interims")
def list_interims(
    user: Annotated[UserMe, Depends(require_permission("organisation.consulter"))],
    statut: str | None = None,
) -> list[dict[str, Any]]:
    where = "WHERE statut = %s" if statut in ("actif", "termine", "annule") else ""
    params: tuple[Any, ...] = (statut,) if where else ()
    rows = db.fetch_all(
        f"SELECT id, fonction_cle, perimetre, titulaire_id, suppleant_id, motif, date_debut, date_fin, statut "
        f"FROM organisation_interim {where} ORDER BY date_debut DESC, cree_le DESC",
        params, role=user.role,
    )
    return [_out(r) for r in rows]


@router.post("/interims", status_code=status.HTTP_201_CREATED)
def create_interim(
    payload: InterimIn, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, Any]:
    fcle = payload.fonction_cle.lower()
    supp = str(payload.suppleant_id)
    titu = str(payload.titulaire_id) if payload.titulaire_id else None
    if not db.fetch_one("SELECT 1 FROM fonction_honorifique WHERE lower(cle) = %s", (fcle,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fonction inconnue au catalogue")
    if not db.fetch_one("SELECT 1 FROM membre WHERE id = %s AND statut = 'actif'", (supp,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="suppléant introuvable ou inactif")
    if titu and not db.fetch_one("SELECT 1 FROM membre WHERE id = %s", (titu,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="titulaire introuvable")
    row = db.execute(
        "INSERT INTO organisation_interim (fonction_cle, perimetre, titulaire_id, suppleant_id, motif, date_debut, date_fin, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, coalesce(%s::date, current_date), %s::date, %s) RETURNING *",
        (fcle, payload.perimetre, titu, supp, payload.motif, payload.date_debut, payload.date_fin, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "organisation_interim_creation", "organisation_interim", str((row or {}).get("id")),
              {"fonction": fcle, "suppleant": supp})
    return _out(row or {})


@router.post("/interims/{interim_id}/terminer")
def terminer_interim(
    interim_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, Any]:
    row = db.execute(
        "UPDATE organisation_interim SET statut = 'termine', date_fin = least(coalesce(date_fin, current_date), current_date), maj_le = now() "
        "WHERE id = %s AND statut = 'actif' RETURNING *",
        (interim_id,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intérim actif introuvable")
    audit.log(user.id, user.role, "organisation_interim_fin", "organisation_interim", interim_id, {})
    return _out(row)


@router.post("/interims/{interim_id}/annuler")
def annuler_interim(
    interim_id: str, user: Annotated[UserMe, Depends(require_permission("organisation.administrer"))],
) -> dict[str, Any]:
    row = db.execute(
        "UPDATE organisation_interim SET statut = 'annule', maj_le = now() WHERE id = %s AND statut = 'actif' RETURNING *",
        (interim_id,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intérim actif introuvable")
    audit.log(user.id, user.role, "organisation_interim_annulation", "organisation_interim", interim_id, {})
    return _out(row)
