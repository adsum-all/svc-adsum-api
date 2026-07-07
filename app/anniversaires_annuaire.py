"""Peer birthday directory for the member calendar (RGPD-safe).

Exposes, per category, the day and month of members' birthdays so the calendar
can overlay them. The birth YEAR is never returned (age stays private), and a
member who opted out of the peer directory is excluded everywhere. VIP birthdays
(members with a confirmed VIP function) are always available; responsables and
own-commission birthdays are also offered so the client can gate them behind the
member's preferences.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from . import db
from .auth import current_user
from .mappers import titre_prefixe
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["anniversaires"])

_BASE_SELECT = (
    "SELECT m.id, m.prenoms, m.nom, m.photo_url, m.genre, m.fonction_confirmee, "
    "extract(month from m.date_naissance)::int AS mois, extract(day from m.date_naissance)::int AS jour, "
    "fh.libelle_h AS fh, fh.libelle_f AS ff, fh.libelle_n AS fn, fh.est_vip AS est_vip, c.nom AS commission "
    "FROM membre m "
    "LEFT JOIN fonction_honorifique fh ON fh.cle = m.fonction_cle "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "WHERE m.date_naissance IS NOT NULL AND m.statut = 'actif' AND m.anniversaire_visible_annuaire = true"
)


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> str:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id


def _row_to_out(r: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(r["id"]),
        "prenoms": r["prenoms"],
        "nom": r["nom"],
        "photo_url": r["photo_url"],
        "jour": r["jour"],
        "mois": r["mois"],
        "commission": r.get("commission"),
        "est_vip": bool(r.get("est_vip")),
        "titre": titre_prefixe(r.get("genre"), r.get("fonction_confirmee"), r.get("fh"), r.get("ff"), r.get("fn")),
    }


# Own-unit overlay categories: each surfaces the birthdays of members sharing the
# caller's own organizational unit. The column on membre carrying that unit.
_UNIT_COLUMN = {
    "commission": "commission_id",
    "tribu": "tribu_id",
    "coordination": "coordination_id",
    "intendance": "intendance_id",
}

# Function-family overlay categories: birthdays of members whose confirmed
# honorific function belongs to a given family of the taxonomy (0092). The map
# turns the member-facing category into the stored fonction_honorifique.famille.
_FAMILLE_CATEGORIE = {
    "direction": "direction",
    "coordinateurs": "coordination",
    "bergers": "bergers",
    "patriarches": "patriarches",
}


@router.get("/membres/anniversaires")
def anniversaires_annuaire(
    membre_id: Annotated[str, Depends(_membre)],
    categorie: Annotated[str, Query(pattern="^(vip|responsables|commission|tribu|coordination|intendance|direction|coordinateurs|bergers|patriarches)$")] = "vip",
    mois: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> list[dict[str, object]]:
    """Peer birthdays for one overlay category. Never exposes the birth year."""
    where = ""
    params: list[object] = []
    if categorie == "vip":
        where = " AND fh.est_vip = true AND m.fonction_confirmee = true"
    elif categorie == "responsables":
        where = " AND (m.fonction_cle = 'responsable' OR m.type_membre = 'responsable')"
    elif categorie in _FAMILLE_CATEGORIE:
        where = " AND fh.famille = %s AND m.fonction_confirmee = true"
        params.append(_FAMILLE_CATEGORIE[categorie])
    else:  # own-unit categories: only members sharing the caller's own unit
        col = _UNIT_COLUMN[categorie]
        own = db.fetch_one(f"SELECT {col} AS unite FROM membre WHERE id = %s", (membre_id,), role=None)
        unite_id = own["unite"] if own else None
        if not unite_id:
            return []
        where = f" AND m.{col} = %s"
        params.append(unite_id)
    if mois is not None:
        where += " AND extract(month from m.date_naissance) = %s"
        params.append(mois)
    sql = _BASE_SELECT + where + " ORDER BY mois, jour, m.prenoms"
    rows = db.fetch_all(sql, tuple(params), role=None)
    return [_row_to_out(r) for r in rows]


class VisibiliteIn(BaseModel):
    visible: bool


@router.put("/membres/me/anniversaire-visibilite")
def set_visibilite(payload: VisibiliteIn, ctx: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Member toggles whether their birthday appears in the peer directory."""
    if not ctx.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    db.execute(
        "UPDATE membre SET anniversaire_visible_annuaire = %s WHERE id = %s",
        (payload.visible, ctx.membre_id),
        role=ctx.role,
    )
    return {"ok": True, "visible": payload.visible}
