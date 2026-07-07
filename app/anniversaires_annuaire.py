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
    "WHERE m.date_naissance IS NOT NULL AND m.statut = 'actif'"
)

# The peer-directory opt-in gates the COLLECTIVE overlays only. A member always
# sees their own birthday on their own calendar (the 'moi' category), even after
# opting out of the directory, so it is never applied to the self view.
_VISIBLE_CLAUSE = " AND m.anniversaire_visible_annuaire = true"

# A member holds SEVERAL functions (membre_fonction, migration 0059), not just the
# primary mirror in membre.fonction_cle. Category filtering must therefore look at
# every active+confirmed function, otherwise a secondary responsibility (e.g. a
# coordinator whose primary function is something else) is missed. These EXISTS
# fragments match a member by their real, confirmed functions.
# Case-insensitive join on the function key, like the canonical resolvers
# (mappers.py, organisation.py) which use lower(mf.fonction_cle), so a legacy
# mixed-case membre_fonction row is never silently missed.
_MF_FAMILLE = (
    "EXISTS (SELECT 1 FROM membre_fonction mf JOIN fonction_honorifique fh2 ON fh2.cle = lower(mf.fonction_cle) "
    "WHERE mf.membre_id = m.id AND mf.actif = true AND mf.confirmee = true AND fh2.famille = %s)"
)
_MF_VIP = (
    "EXISTS (SELECT 1 FROM membre_fonction mf JOIN fonction_honorifique fh2 ON fh2.cle = lower(mf.fonction_cle) "
    "WHERE mf.membre_id = m.id AND mf.actif = true AND mf.confirmee = true AND fh2.est_vip = true)"
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
    "patriarches": "patriarches",
}


def _categorie_where(categorie: str, membre_id: str) -> tuple[str, list[object]] | None:
    """WHERE suffix and params for a category, or None for an own-unit category
    (which needs a DB lookup done by the caller).

    The 'moi' (self) category matches the caller and deliberately OMITS the
    peer-directory visibility clause, so a member always sees their own birthday
    on their own calendar even after opting out of the directory. Every collective
    category keeps the visibility clause (opt-out members stay hidden from peers)."""
    if categorie == "moi":
        return " AND m.id = %s", [membre_id]
    # Every function-based category is ADDITIVE: the legacy primary-function check
    # (which already worked) OR the full membre_fonction set (which adds secondary
    # functions). This never removes anyone the previous query matched.
    if categorie == "vip":
        return _VISIBLE_CLAUSE + " AND ((fh.est_vip = true AND m.fonction_confirmee = true) OR " + _MF_VIP + ")", []
    if categorie == "responsables":
        return _VISIBLE_CLAUSE + " AND (m.fonction_cle = 'responsable' OR m.type_membre = 'responsable' OR " + _MF_FAMILLE + ")", ["responsables"]
    if categorie == "bergers":
        # Berger/Bergere is the consecration title (est_berger). Also match a
        # confirmed 'bergers'-family function (e.g. Berger des missions), primary
        # or secondary via membre_fonction, so no shepherd is ever missed.
        return _VISIBLE_CLAUSE + " AND (m.est_berger = true OR " + _MF_FAMILLE + ")", ["bergers"]
    if categorie in _FAMILLE_CATEGORIE:  # direction, coordinateurs (-> coordination), patriarches
        fam = _FAMILLE_CATEGORIE[categorie]
        return _VISIBLE_CLAUSE + " AND ((fh.famille = %s AND m.fonction_confirmee = true) OR " + _MF_FAMILLE + ")", [fam, fam]
    return None


@router.get("/membres/anniversaires")
def anniversaires_annuaire(
    membre_id: Annotated[str, Depends(_membre)],
    categorie: Annotated[str, Query(pattern="^(moi|vip|responsables|commission|tribu|coordination|intendance|direction|coordinateurs|bergers|patriarches)$")] = "vip",
    mois: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> list[dict[str, object]]:
    """Birthdays for one calendar overlay category. Never exposes the birth year."""
    built = _categorie_where(categorie, membre_id)
    if built is not None:
        where, params = built[0], list(built[1])
    else:  # own-unit categories: only members sharing the caller's own unit
        col = _UNIT_COLUMN[categorie]
        own = db.fetch_one(f"SELECT {col} AS unite FROM membre WHERE id = %s", (membre_id,), role=None)
        unite_id = own["unite"] if own else None
        if not unite_id:
            return []
        where = _VISIBLE_CLAUSE + f" AND m.{col} = %s"
        params = [unite_id]
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
