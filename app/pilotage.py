"""Pilotage layer for responsables de perimetre (foundation).

Every endpoint here is bounded to the caller's resolved scope (see perimetre.py):
a responsable only ever reads the members and activities of their own perimeter.
This first slice proves the cloisonnement and the agenda diffusion filter; the
richer modules (consultations, dashboards, exports) build on the same scope.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from . import audit, db, identite
from .fields import LineStr, ShortStr, TitleStr
from .perimetre import PerimetreContext, Scope, require_perimetre

router = APIRouter(prefix="/api/v1/pilotage", tags=["pilotage"])


def _cible_autorisee(scope: Scope, cible_type: str, cible_id: str | None) -> bool:
    """Whether the responsable may publish to this target: only a unit inside
    their perimeter. A general broadcast is reserved to global oversight roles."""
    if cible_type == "general":
        return scope.is_global
    return scope.couvre(cible_type, cible_id)


def _agenda_predicate(scope: Scope) -> tuple[str, list[object]]:
    """SQL fragment selecting the events that reach this scope: general ones plus
    those targeting a unit inside the scope (targeting is the diffusion boundary)."""
    if scope.is_global:
        return "TRUE", []
    clauses = ["e.cible_type = 'general'"]
    params: list[object] = []
    for cible, ids in (
        ("coordination", scope.coordination_ids),
        ("intendance", scope.intendance_ids),
        ("tribu", scope.tribu_ids),
    ):
        if ids:
            clauses.append(f"(e.cible_type = '{cible}' AND e.cible_id = ANY(%s::uuid[]))")
            params.append(list(ids))
    return "(" + " OR ".join(clauses) + ")", params


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
    where, params = _agenda_predicate(ctx.scope)
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


class CreateActivite(BaseModel):
    """A perimeter activity: its target must fall inside the caller's scope."""

    titre: TitleStr
    type: ShortStr | None = None
    debut: str  # ISO datetime
    fin: str | None = None
    lieu: LineStr | None = None
    mode: ShortStr | None = None  # presentiel | en_ligne | hybride
    cible_type: ShortStr = "general"  # general | coordination | intendance | tribu
    cible_id: ShortStr | None = None
    visibilite: ShortStr = "membres"  # public | membres | prive


@router.post("/activites", status_code=status.HTTP_201_CREATED)
def creer_activite(payload: CreateActivite, ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, str]:
    """Create an activity bounded to the caller's perimeter.

    A responsable can only target a unit inside their scope; broadcasting to
    everyone (general) stays reserved to global oversight roles. The event feeds
    the member agenda through the existing targeting rules once published.
    """
    if payload.cible_type not in ("general", "coordination", "intendance", "tribu"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_type invalide")
    if payload.cible_type != "general" and not payload.cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id requis pour une activite ciblee")
    if not _cible_autorisee(ctx.scope, payload.cible_type, payload.cible_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cible hors de votre perimetre")
    cible_id = payload.cible_id if payload.cible_type != "general" else None
    created = db.execute(
        "INSERT INTO evenement (titre, type, mode, debut, fin, lieu, cible_type, cible_id, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (payload.titre, payload.type, payload.mode, payload.debut, payload.fin, payload.lieu,
         payload.cible_type, cible_id, payload.visibilite, ctx.user.id),
        role=ctx.user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation impossible")
    audit.log(ctx.user.id, ctx.user.role, "creation_activite_perimetre", "evenement", str(created["id"]),
              {"cible_type": payload.cible_type, "cible_id": cible_id})
    return {"id": str(created["id"])}


@router.get("/tableau-de-bord")
def tableau_de_bord(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, object]:
    """Scoped KPI summary for the responsable's perimeter."""
    where, params = ctx.scope.membre_predicate("m")
    stats = db.fetch_one(
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE m.statut = 'actif') AS actifs, "
        f"count(*) FILTER (WHERE m.verifie) AS verifies "
        f"FROM membre m WHERE {where}",
        tuple(params),
        role=ctx.user.role,
    ) or {}
    ev_where, ev_params = _agenda_predicate(ctx.scope)
    a_venir = db.fetch_one(
        f"SELECT count(*) AS n FROM evenement e "
        f"WHERE ({ev_where}) AND e.visibilite IN ('public', 'membres') AND e.debut >= now()",
        tuple(ev_params),
        role=ctx.user.role,
    ) or {}
    return {
        "membres_total": int(stats.get("total") or 0),
        "membres_actifs": int(stats.get("actifs") or 0),
        "membres_verifies": int(stats.get("verifies") or 0),
        "activites_a_venir": int(a_venir.get("n") or 0),
    }


@router.get("/export/membres.csv")
def export_membres_csv(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> Response:
    """CSV export of the perimeter's members (minimal fields), scope-bounded and
    journaled. A responsable never exports data outside their perimeter."""
    where, params = ctx.scope.membre_predicate("m")
    rows = db.fetch_all(
        f"SELECT m.matricule, m.prenoms, m.nom, m.statut, m.verifie FROM membre m WHERE {where} "
        f"ORDER BY m.nom ASC, m.prenoms ASC LIMIT 5000",
        tuple(params),
        role=ctx.user.role,
    )
    lignes = ["matricule,nom_affichage,statut,verifie"]
    for r in rows:
        nom = (identite.nom_affichage(r.get("nom"), r.get("prenoms")) or "").replace('"', "'")
        lignes.append(f'{r.get("matricule") or ""},"{nom}",{r.get("statut") or ""},{bool(r.get("verifie"))}')
    audit.log(ctx.user.id, ctx.user.role, "export_membres_perimetre", "membre", None, {"lignes": len(rows)})
    return Response(
        content="\n".join(lignes),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=membres-perimetre.csv"},
    )
