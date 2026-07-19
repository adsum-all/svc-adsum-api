# ruff: noqa: E501
"""Member activities: past-activity history (survey based) and access controls.

Two clearly separated reads, matching the two clearly separated data sources:

- ``/me/activites/passees`` is the REAL activity history: terminated events the
  member could see, each carrying the member's OFFICIAL participation status taken
  from the ``participation`` survey (present, partial, absent, did not answer,
  survey closed without answer). This feeds statistics.

- ``/me/activites/controles`` is the identity-check trail: rows from ``presence``
  (a QR or manual scan at an entrance). A scan proves an identity check happened,
  NOT participation, and never drives the official presence statistics. Each row
  carries a reminder to that effect.

Both are server-paginated and filterable so they stay usable over years of data.
The existing ``/me/evenements`` (ongoing and upcoming) and ``/me/historique`` are
left untouched for backward compatibility.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import db, visibilite
from .membres import require_membre

router = APIRouter(prefix="/api/v1/membres/me", tags=["activites-membre"])

# A survey is considered closed this long after the event ends (per-event override,
# else a safe default). Mirrors participation.FENETRE semantics without importing it.
_FENETRE_FERMEE_SQL = "now() > COALESCE(e.fin, e.debut) + make_interval(hours => COALESCE(e.fenetre_reponse_heures, 5))"


def _statut_personnel(statut: str | None, modalite: str | None, has_row: bool, fenetre_fermee: bool) -> str:
    if statut == "present":
        return "participe_en_ligne" if modalite == "en_ligne" else "present"
    if statut == "partiel":
        return "partiel"
    if statut == "absent":
        return "absent"
    if not has_row and fenetre_fermee:
        return "cloture_sans_reponse"
    return "non_repondu"


class ActivitePassee(BaseModel):
    id: str
    titre: str
    type: str | None
    couleur: str | None
    mode: str | None
    debut: str | None
    fin: str | None
    lieu: str | None
    statut_personnel: str
    source: str | None
    repondu_le: str | None


class ActivitesPasseesOut(BaseModel):
    items: list[ActivitePassee]
    total: int
    page: int
    pages: int
    limit: int


@router.get("/activites/passees", response_model=ActivitesPasseesOut)
def activites_passees(
    ctx: Annotated[tuple[str, str], Depends(require_membre)],
    mois: int | None = Query(default=None, ge=1, le=12),
    annee: int | None = Query(default=None, ge=2020, le=2100),
    type_id: str | None = Query(default=None),
    statut: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> ActivitesPasseesOut:
    """Terminated activities visible to the member, with the official survey status."""
    membre_id, role = ctx
    where = (
        "WHERE COALESCE(e.fin, e.debut + interval '2 hours') < now() "
        "AND (e.annule = false OR e.annule IS NULL) AND "
        + visibilite.CIBLE_PREDICATE
    )
    params: list[Any] = [membre_id]
    if mois:
        where += " AND EXTRACT(MONTH FROM e.debut) = %s"
        params.append(mois)
    if annee:
        where += " AND EXTRACT(YEAR FROM e.debut) = %s"
        params.append(annee)
    if type_id:
        where += " AND e.type_evenement_id = %s"
        params.append(type_id)
    if statut == "present":
        where += " AND p.statut = 'present'"
    elif statut == "absent":
        where += " AND p.statut = 'absent'"
    elif statut == "partiel":
        where += " AND p.statut = 'partiel'"
    elif statut == "non_repondu":
        where += " AND p.id IS NULL"
    if q:
        where += " AND e.titre ILIKE %s"
        params.append(f"%{q}%")

    # `mi` (the member's stewardship) is required by the shared visibility predicate,
    # exactly like the ongoing/upcoming feed in membres.my_events.
    base_from = """
        FROM evenement e
        JOIN membre m ON m.id = %s
        LEFT JOIN intendance mi ON mi.id = m.intendance_id
        LEFT JOIN type_evenement te ON te.id = e.type_evenement_id
        LEFT JOIN participation p ON p.evenement_id = e.id AND p.membre_id = m.id
    """
    total_row = db.fetch_one(f"SELECT count(*) AS c {base_from} {where}", tuple(params), role=role)
    total = int(total_row["c"]) if total_row else 0
    rows = db.fetch_all(
        f"""
        SELECT e.id, e.titre, COALESCE(te.nom, e.type) AS type_nom, te.couleur, e.mode, e.debut, e.fin, e.lieu,
               p.id AS participation_id, p.statut AS p_statut, p.source AS p_source, p.modalite AS p_modalite,
               p.maj_le AS p_maj_le, ({_FENETRE_FERMEE_SQL}) AS fenetre_fermee
        {base_from}
        {where}
        ORDER BY e.debut DESC
        LIMIT %s OFFSET %s
        """,
        (*params, limit, offset),
        role=role,
    )
    items = [
        ActivitePassee(
            id=str(r["id"]), titre=r["titre"], type=r.get("type_nom"), couleur=r.get("couleur"), mode=r.get("mode"),
            debut=r["debut"].isoformat() if r.get("debut") else None,
            fin=r["fin"].isoformat() if r.get("fin") else None,
            lieu=r.get("lieu"),
            statut_personnel=_statut_personnel(r.get("p_statut"), r.get("p_modalite"), r.get("participation_id") is not None, bool(r.get("fenetre_fermee"))),
            source=r.get("p_source"),
            repondu_le=r["p_maj_le"].isoformat() if r.get("p_maj_le") else None,
        )
        for r in rows
    ]
    pages = max(1, (total + limit - 1) // limit)
    return ActivitesPasseesOut(items=items, total=total, page=(offset // limit) + 1, pages=pages, limit=limit)


class ControleAcces(BaseModel):
    evenement_id: str
    evenement_titre: str | None
    debut: str | None
    lieu: str | None
    arrivee: str | None
    depart: str | None
    mode: str | None
    methode: str | None
    resultat: str


class ControlesOut(BaseModel):
    items: list[ControleAcces]
    total: int
    page: int
    pages: int
    limit: int


@router.get("/activites/controles", response_model=ControlesOut)
def controles_acces(
    ctx: Annotated[tuple[str, str], Depends(require_membre)],
    limit: int = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> ControlesOut:
    """Identity-check trail (scans). A scan is a verification event, not a proof of
    participation: official participation always comes from the survey."""
    membre_id, role = ctx
    total_row = db.fetch_one("SELECT count(*) AS c FROM presence WHERE membre_id = %s", (membre_id,), role=role)
    total = int(total_row["c"]) if total_row else 0
    rows = db.fetch_all(
        """
        SELECT p.evenement_id, e.titre AS evenement_titre, e.debut, e.lieu,
               p.arrivee, p.depart, p.mode, p.methode
        FROM presence p
        JOIN evenement e ON e.id = p.evenement_id
        WHERE p.membre_id = %s
        ORDER BY p.arrivee DESC NULLS LAST
        LIMIT %s OFFSET %s
        """,
        (membre_id, limit, offset),
        role=role,
    )
    items = [
        ControleAcces(
            evenement_id=str(r["evenement_id"]), evenement_titre=r.get("evenement_titre"),
            debut=r["debut"].isoformat() if r.get("debut") else None,
            lieu=r.get("lieu"),
            arrivee=r["arrivee"].isoformat() if r.get("arrivee") else None,
            depart=r["depart"].isoformat() if r.get("depart") else None,
            mode=r.get("mode"), methode=r.get("methode"),
            resultat="identite_verifiee",
        )
        for r in rows
    ]
    pages = max(1, (total + limit - 1) // limit)
    return ControlesOut(items=items, total=total, page=(offset // limit) + 1, pages=pages, limit=limit)
