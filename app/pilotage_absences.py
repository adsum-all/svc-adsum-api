"""Deciding whether an absence is excused, which only a responsible person may do.

A member says why they were not there. That is a request, not a verdict, and the
distinction is the one the organisation was most explicit about: nobody excuses
themselves. Until now the distinction could not even be expressed, because there was
no reason, no decision, no decider, and no screen on which to take one. The rule was
unbreakable only because it was unimplementable.

Three properties this rests on.

The decision is bounded to the caller's perimeter. A coordination lead qualifies the
absences of their own members, and the predicate comes from the same scope resolver
every other pilotage endpoint uses, so there is one definition of "my members" rather
than one per screen.

The decision is never anonymous. The schema refuses a qualification that carries no
decider and no date, so the trace cannot be omitted by a code path that forgets. The
audit entry names the actor, the member, the activity, the previous state and the new
one, because a decision somebody contests months later has to be reconstructable.

A decision can be revisited, and revisiting is itself a decision. Reopening writes a
new audit entry rather than erasing the old one: the history of a contested absence
is exactly what makes it defensible.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import audit, db, identite
from .perimetre import PerimetreContext, require_perimetre

router = APIRouter(prefix="/api/v1/pilotage", tags=["pilotage"])

#: The states an absence can be in. `sans_objet` covers everyone who did follow.
_QUALIFICATIONS = ("en_attente", "excusee", "non_excusee")

_TRIS = {
    "recent": "e.debut DESC",
    "ancien": "e.debut ASC",
    "nom": "m.nom ASC, m.prenoms ASC",
}


class DecisionIn(BaseModel):
    qualification: str = Field(description="excusee, non_excusee ou en_attente")
    commentaire: str | None = Field(default=None, max_length=1000)


def _nom(r: dict[str, Any]) -> str:
    return identite.nom_affichage(r.get("nom"), r.get("prenoms")) or str(r.get("matricule") or "Membre")


def lister(
    ctx: PerimetreContext,
    qualification: str | None,
    tri: str,
    limite: int,
    decalage: int,
) -> dict[str, Any]:
    """The absences of this perimeter awaiting or carrying a decision.

    Paginated on the server. A responsible person with several hundred members would
    otherwise receive every absence ever declared in one response, and the browser
    would hide what it could not show.
    """
    where, params = ctx.scope.membre_predicate("m")
    conditions = [where, "p.statut = 'absent'", "NOT coalesce(p.legacy_ambigu, false)"]
    valeurs = list(params)

    if qualification in _QUALIFICATIONS:
        conditions.append("p.absence_qualification = %s")
        valeurs.append(qualification)
    else:
        # Anything the organisation could act on. A member who declined to give a
        # reason asked for nothing and is not in a queue of pending decisions.
        conditions.append("p.absence_qualification <> 'sans_objet'")

    clause = " AND ".join(conditions)
    ordre = _TRIS.get(tri, _TRIS["recent"])
    limite = max(1, min(int(limite), 100))
    decalage = max(0, int(decalage))

    lignes = db.fetch_all(
        "SELECT p.evenement_id, p.membre_id, p.absence_motif, p.absence_commentaire, "
        "p.absence_qualification, p.qualifie_le, p.qualification_commentaire, p.maj_le, "
        "m.matricule, m.nom, m.prenoms, "
        "e.titre AS activite, e.debut, "
        "coalesce(ma.libelle, 'Non précisé') AS motif_libelle, "
        "u.email AS decideur, "
        "count(*) OVER () AS total "
        "FROM participation p "
        "JOIN membre m ON m.id = p.membre_id "
        "JOIN evenement e ON e.id = p.evenement_id "
        "LEFT JOIN motif_absence ma ON ma.code = p.absence_motif "
        "LEFT JOIN utilisateur u ON u.id = p.qualifie_par "
        f"WHERE {clause} ORDER BY {ordre} LIMIT %s OFFSET %s",
        (*valeurs, limite, decalage),
        role=ctx.user.role,
    )

    return {
        "absences": [
            {
                "evenement_id": str(r["evenement_id"]),
                "membre_id": str(r["membre_id"]),
                "membre": _nom(r),
                "matricule": r.get("matricule"),
                "activite": r.get("activite"),
                "date": r["debut"].isoformat() if r.get("debut") else None,
                "motif": r.get("absence_motif"),
                "motif_libelle": r.get("motif_libelle"),
                "commentaire": r.get("absence_commentaire"),
                "qualification": r.get("absence_qualification"),
                "decide_le": r["qualifie_le"].isoformat() if r.get("qualifie_le") else None,
                "decideur": r.get("decideur"),
                "decision_commentaire": r.get("qualification_commentaire"),
                "declare_le": r["maj_le"].isoformat() if r.get("maj_le") else None,
            }
            for r in lignes
        ],
        "total": int(lignes[0]["total"]) if lignes else 0,
        "limite": limite,
        "decalage": decalage,
    }


def decider(
    ctx: PerimetreContext, evenement_id: str, membre_id: str, decision: DecisionIn
) -> dict[str, Any]:
    """Record a decision on one absence, inside the caller's perimeter.

    The perimeter is enforced in the UPDATE itself rather than checked beforehand: a
    separate check leaves a window in which the member could move out of scope between
    the check and the write, and it is the kind of window nobody notices until it is
    used.
    """
    if decision.qualification not in _QUALIFICATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Décision inconnue. Attendu : {', '.join(_QUALIFICATIONS)}.",
        )

    where, params = ctx.scope.membre_predicate("m")
    avant = db.fetch_one(
        f"SELECT p.absence_qualification, p.absence_motif FROM participation p "
        f"JOIN membre m ON m.id = p.membre_id "
        f"WHERE p.evenement_id = %s AND p.membre_id = %s AND {where}",
        (evenement_id, membre_id, *params),
        role=ctx.user.role,
    )
    if not avant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Absence introuvable dans votre périmètre.",
        )
    if avant["absence_qualification"] == "sans_objet":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cette participation n'est pas une absence : il n'y a rien à qualifier.",
        )

    # Closing the file records who closed it and when; reopening clears both, because
    # a pending absence still carrying somebody's name reads as decided.
    #
    # The decider and the timestamp are computed in Python rather than with a CASE, so
    # the statement keeps the positional placeholders the scope predicate produces.
    # Mixing the two placeholder styles in one statement is not allowed by the driver.
    ferme = decision.qualification in ("excusee", "non_excusee")
    maj = db.fetch_one(
        f"UPDATE participation p SET absence_qualification = %s, "
        f"qualifie_par = %s::uuid, "
        f"qualifie_le = CASE WHEN %s THEN now() ELSE NULL END, "
        f"qualification_commentaire = %s "
        f"FROM membre m WHERE m.id = p.membre_id "
        f"AND p.evenement_id = %s AND p.membre_id = %s AND {where} "
        f"RETURNING p.absence_qualification",
        (
            decision.qualification,
            ctx.user.id if ferme else None,
            ferme,
            decision.commentaire,
            evenement_id,
            membre_id,
            *params,
        ),
        role=ctx.user.role,
    )
    if not maj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Absence introuvable dans votre périmètre.",
        )

    audit.log(
        ctx.user.id, ctx.user.role, "qualification_absence", "participation", None,
        {
            "evenement_id": evenement_id,
            "membre_id": membre_id,
            "motif": avant.get("absence_motif"),
            "avant": avant["absence_qualification"],
            "apres": decision.qualification,
            "commentaire": (decision.commentaire or "")[:200],
        },
    )
    return {
        "evenement_id": evenement_id,
        "membre_id": membre_id,
        "qualification": maj["absence_qualification"],
        "avant": avant["absence_qualification"],
    }


@router.get("/absences")
def lister_absences(
    ctx: Annotated[PerimetreContext, Depends(require_perimetre)],
    qualification: str | None = Query(default=None, description="en_attente, excusee, non_excusee"),
    tri: str = Query(default="recent"),
    limite: int = Query(default=20, ge=1, le=100),
    decalage: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Absences declared inside this perimeter, with their reason and their decision."""
    return lister(ctx, qualification, tri, limite, decalage)


@router.get("/absences/synthese")
def synthese_absences(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, Any]:
    """How many absences are waiting, excused, refused, and on what reasons.

    Counts rather than rates alone: a percentage without the number behind it is what
    lets a leader act on three people as if they were three hundred.
    """
    where, params = ctx.scope.membre_predicate("m")
    r = db.fetch_one(
        "SELECT "
        "count(*) FILTER (WHERE p.absence_qualification = 'en_attente') AS en_attente, "
        "count(*) FILTER (WHERE p.absence_qualification = 'excusee') AS excusees, "
        "count(*) FILTER (WHERE p.absence_qualification = 'non_excusee') AS non_excusees, "
        "count(*) FILTER (WHERE p.statut = 'absent') AS absences_totales "
        "FROM participation p JOIN membre m ON m.id = p.membre_id "
        f"WHERE {where} AND NOT coalesce(p.legacy_ambigu, false)",
        tuple(params), role=ctx.user.role,
    ) or {}

    motifs = db.fetch_all(
        "SELECT coalesce(ma.libelle, 'Non précisé') AS libelle, count(*) AS n "
        "FROM participation p JOIN membre m ON m.id = p.membre_id "
        "LEFT JOIN motif_absence ma ON ma.code = p.absence_motif "
        f"WHERE {where} AND p.statut = 'absent' AND NOT coalesce(p.legacy_ambigu, false) "
        "GROUP BY 1 ORDER BY 2 DESC",
        tuple(params), role=ctx.user.role,
    )

    total_qualifiables = int(r.get("en_attente") or 0) + int(r.get("excusees") or 0) + int(r.get("non_excusees") or 0)
    return {
        "en_attente": int(r.get("en_attente") or 0),
        "excusees": int(r.get("excusees") or 0),
        "non_excusees": int(r.get("non_excusees") or 0),
        "absences_totales": int(r.get("absences_totales") or 0),
        "avec_motif": total_qualifiables,
        "taux_excusees": (
            round(100.0 * int(r.get("excusees") or 0) / total_qualifiables, 1)
            if total_qualifiables else 0.0
        ),
        "par_motif": [{"libelle": str(m["libelle"]), "nombre": int(m["n"])} for m in motifs],
    }


@router.put("/absences/{evenement_id}/{membre_id}")
def qualifier_absence(
    evenement_id: str,
    membre_id: str,
    decision: DecisionIn,
    ctx: Annotated[PerimetreContext, Depends(require_perimetre)],
) -> dict[str, Any]:
    """Excuse an absence, refuse it, or send it back for review.

    The only way an absence becomes excused. A member can ask; they can never decide.
    """
    return decider(ctx, evenement_id, membre_id, decision)
