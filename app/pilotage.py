"""Pilotage layer for responsables de perimetre (foundation).

Every endpoint here is bounded to the caller's resolved scope (see perimetre.py):
a responsable only ever reads the members and activities of their own perimeter.
This first slice proves the cloisonnement and the agenda diffusion filter; the
richer modules (consultations, dashboards, exports) build on the same scope.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import db, identite
from .perimetre import PerimetreContext, require_perimetre

router = APIRouter(prefix="/api/v1/pilotage", tags=["pilotage"])


@router.get("/moi")
def moi(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, object]:
    """Scope summary of the current responsable, and the size of their perimeter.

    Proves that the scope is derived and bounded: the member count is exactly the
    caller's perimeter, never the whole base (unless a global oversight role).
    """
    scope = ctx.scope
    where, params = scope.membre_predicate("m")
    row = db.fetch_one(
        f"SELECT count(*) AS n FROM membre m WHERE {where}",
        tuple(params),
        role=ctx.user.role,
    )
    return {
        "role": ctx.user.role,
        "global": scope.is_global,
        "coordinations": len(scope.coordination_ids),
        "intendances": len(scope.intendance_ids),
        "tribus": len(scope.tribu_ids),
        "membres_perimetre": int((row or {}).get("n") or 0),
    }


@router.get("/membres")
def membres(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> list[dict[str, object]]:
    """Members of the caller's perimeter, minimal fields only (relance-safe).

    Deliberately excludes the full civil identity, address, phone and e-mail: a
    responsable pilots and follows up, they do not read the members' complete
    personal record (that stays with the member-managing back-office roles).
    """
    where, params = ctx.scope.membre_predicate("m")
    rows = db.fetch_all(
        f"SELECT m.id, m.matricule, m.prenoms, m.nom, m.nom_affiche, m.statut, m.verifie "
        f"FROM membre m WHERE {where} ORDER BY m.nom ASC, m.prenoms ASC LIMIT 500",
        tuple(params),
        role=ctx.user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "matricule": r.get("matricule"),
            "nom_affichage": identite.nom_affichage(r.get("nom"), r.get("prenoms")),
            "statut": r.get("statut"),
            "verifie": bool(r.get("verifie")),
        }
        for r in rows
    ]


@router.get("/agenda")
def agenda(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> list[dict[str, object]]:
    """Published activities reaching the caller's perimeter.

    General activities plus those targeting a unit inside the scope, applying the
    same diffusion rule the member agenda uses (targeting is the authoritative
    boundary; a member never sees an activity aimed at another perimeter).
    """
    scope = ctx.scope
    if scope.is_global:
        where, params = "TRUE", []
    else:
        clauses = ["e.cible_type = 'general'"]
        params = []
        for cible, ids in (
            ("coordination", scope.coordination_ids),
            ("intendance", scope.intendance_ids),
            ("tribu", scope.tribu_ids),
        ):
            if ids:
                clauses.append(f"(e.cible_type = '{cible}' AND e.cible_id = ANY(%s::uuid[]))")
                params.append(list(ids))
        where = "(" + " OR ".join(clauses) + ")"
    rows = db.fetch_all(
        f"SELECT e.id, e.titre, e.type, e.debut, e.fin, e.lieu, e.mode, e.cible_type, e.cible_id, e.visibilite "
        f"FROM evenement e WHERE ({where}) AND e.visibilite IN ('public', 'membres') "
        f"ORDER BY e.debut DESC LIMIT 200",
        tuple(params),
        role=ctx.user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "titre": r.get("titre"),
            "type": r.get("type"),
            "debut": r["debut"].isoformat() if r.get("debut") else None,
            "fin": r["fin"].isoformat() if r.get("fin") else None,
            "lieu": r.get("lieu"),
            "mode": r.get("mode"),
            "cible_type": r.get("cible_type"),
        }
        for r in rows
    ]
