"""Perimeter consultations (surveys): create, publish, respond, aggregate.

A responsable de perimetre creates a consultation targeting a unit inside their
scope; members of that unit answer once (anti-doublon by a unique constraint);
results are aggregated within the scope. Reuses the perimeter scope for the
security boundary. Notifications reuse the existing notifier when a consultation
is published (best effort, never blocking).
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .auth import current_user
from .fields import LineStr, LongTextStr, ShortStr, TitleStr
from .perimetre import PerimetreContext, require_perimetre
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["consultations"])

_QUESTION_TYPES = ("oui_non", "choix", "choix_multiple", "note", "texte")


class QuestionIn(BaseModel):
    libelle: TitleStr
    type: ShortStr = "choix"
    options: list[LineStr] = Field(default_factory=list, max_length=30)
    ordre: int = 0


class ConsultationIn(BaseModel):
    titre: TitleStr
    description: LongTextStr | None = None
    dimension: ShortStr = "general"  # general | coordination | intendance | tribu
    scope_id: ShortStr | None = None
    evenement_id: ShortStr | None = None
    questions: list[QuestionIn] = Field(default_factory=list, max_length=30)


class ReponseItem(BaseModel):
    question_id: ShortStr
    valeur: LongTextStr | None = None


class ReponsesIn(BaseModel):
    reponses: list[ReponseItem] = Field(default_factory=list, max_length=30)


def _peut_gerer(ctx: PerimetreContext, dimension: str, scope_id: str | None) -> bool:
    """Whether the responsable may manage a consultation on this target."""
    if dimension == "general":
        return ctx.scope.is_global
    return ctx.scope.couvre(dimension, scope_id)


# --- Responsable side -------------------------------------------------------

@router.post("/pilotage/consultations", status_code=status.HTTP_201_CREATED)
def creer_consultation(payload: ConsultationIn, ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, str]:
    """Create a consultation (draft) targeting a unit inside the caller's scope."""
    if payload.dimension not in ("general", "coordination", "intendance", "tribu"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="dimension invalide")
    if payload.dimension != "general" and not payload.scope_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="scope_id requis")
    if not _peut_gerer(ctx, payload.dimension, payload.scope_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cible hors de votre perimetre")
    scope_id = payload.scope_id if payload.dimension != "general" else None
    created = db.execute(
        "INSERT INTO consultation (titre, description, dimension, scope_id, evenement_id, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (payload.titre, payload.description, payload.dimension, scope_id, payload.evenement_id, ctx.user.id),
        role=ctx.user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation impossible")
    cid = str(created["id"])
    for q in payload.questions:
        qtype = q.type if q.type in _QUESTION_TYPES else "choix"
        db.execute(
            "INSERT INTO consultation_question (consultation_id, libelle, type, options, ordre) VALUES (%s, %s, %s, %s::jsonb, %s)",
            (cid, q.libelle, qtype, json.dumps(list(q.options)), q.ordre),
            role=ctx.user.role,
        )
    audit.log(ctx.user.id, ctx.user.role, "creation_consultation", "consultation", cid, {"dimension": payload.dimension})
    return {"id": cid}


@router.post("/pilotage/consultations/{consultation_id}/statut")
def changer_statut(consultation_id: str, ctx: Annotated[PerimetreContext, Depends(require_perimetre)], statut: str) -> dict[str, str]:
    """Open (ouverte), close (close) or archive a consultation of the caller's scope."""
    if statut not in ("ouverte", "close", "archivee"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut invalide")
    row = db.fetch_one("SELECT dimension, scope_id FROM consultation WHERE id = %s", (consultation_id,), role=ctx.user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="consultation introuvable")
    if not _peut_gerer(ctx, str(row["dimension"]), str(row["scope_id"]) if row.get("scope_id") else None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="hors de votre perimetre")
    if statut == "ouverte":
        db.execute("UPDATE consultation SET statut = %s, ouverture = coalesce(ouverture, now()) WHERE id = %s", (statut, consultation_id), role=ctx.user.role)
    else:
        db.execute("UPDATE consultation SET statut = %s, fermeture = CASE WHEN %s = 'close' THEN now() ELSE fermeture END WHERE id = %s", (statut, statut, consultation_id), role=ctx.user.role)
    audit.log(ctx.user.id, ctx.user.role, "statut_consultation", "consultation", consultation_id, {"statut": statut})
    return {"id": consultation_id, "statut": statut}


@router.get("/pilotage/consultations")
def lister_consultations(ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> list[dict[str, object]]:
    """Consultations the caller may manage (their scope, plus general if global)."""
    rows = db.fetch_all(
        "SELECT id, titre, dimension, scope_id, statut, evenement_id, cree_le FROM consultation ORDER BY cree_le DESC LIMIT 200",
        (),
        role=ctx.user.role,
    )
    out = []
    for r in rows:
        if not _peut_gerer(ctx, str(r["dimension"]), str(r["scope_id"]) if r.get("scope_id") else None):
            continue
        out.append({
            "id": str(r["id"]), "titre": r["titre"], "dimension": r["dimension"],
            "statut": r["statut"], "evenement_id": str(r["evenement_id"]) if r.get("evenement_id") else None,
        })
    return out


@router.get("/pilotage/consultations/{consultation_id}/resultats")
def resultats(consultation_id: str, ctx: Annotated[PerimetreContext, Depends(require_perimetre)]) -> dict[str, object]:
    """Aggregated results of a consultation of the caller's scope."""
    cons = db.fetch_one("SELECT dimension, scope_id, titre FROM consultation WHERE id = %s", (consultation_id,), role=ctx.user.role)
    if not cons:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="consultation introuvable")
    if not _peut_gerer(ctx, str(cons["dimension"]), str(cons["scope_id"]) if cons.get("scope_id") else None):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="hors de votre perimetre")
    questions = db.fetch_all(
        "SELECT id, libelle, type FROM consultation_question WHERE consultation_id = %s ORDER BY ordre ASC",
        (consultation_id,), role=ctx.user.role,
    )
    result_questions = []
    for q in questions:
        agg = db.fetch_all(
            "SELECT valeur, count(*) AS n FROM consultation_reponse WHERE question_id = %s GROUP BY valeur ORDER BY n DESC",
            (q["id"],), role=ctx.user.role,
        )
        result_questions.append({
            "id": str(q["id"]), "libelle": q["libelle"], "type": q["type"],
            "reponses": [{"valeur": a.get("valeur"), "n": int(a["n"])} for a in agg],
        })
    repondants = db.fetch_one(
        "SELECT count(DISTINCT membre_id) AS n FROM consultation_reponse WHERE consultation_id = %s",
        (consultation_id,), role=ctx.user.role,
    ) or {}
    return {"titre": cons["titre"], "repondants": int(repondants.get("n") or 0), "questions": result_questions}


# --- Member side ------------------------------------------------------------

def _membre_axes(membre_id: str, role: str) -> dict[str, object]:
    return db.fetch_one(
        "SELECT coordination_id, intendance_id, tribu_id FROM membre WHERE id = %s",
        (membre_id,), role=role,
    ) or {}


def _visibilite_predicate(axes: dict[str, object]) -> tuple[str, list[object]]:
    """A member sees a general consultation, or one whose target is exactly their
    own coordination/intendance/tribu."""
    clauses = ["c.dimension = 'general'"]
    params: list[object] = []
    for dim, col in (("coordination", "coordination_id"), ("intendance", "intendance_id"), ("tribu", "tribu_id")):
        if axes.get(col):
            clauses.append(f"(c.dimension = '{dim}' AND c.scope_id = %s)")
            params.append(str(axes[col]))
    return "(" + " OR ".join(clauses) + ")", params


@router.get("/membres/me/consultations")
def mes_consultations(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """Open consultations addressed to the member (general or their exact unit)."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lie a un membre")
    axes = _membre_axes(user.membre_id, user.role)
    where, params = _visibilite_predicate(axes)
    rows = db.fetch_all(
        f"SELECT c.id, c.titre, c.description, c.evenement_id FROM consultation c "
        f"WHERE {where} AND c.statut = 'ouverte' ORDER BY c.cree_le DESC LIMIT 100",
        tuple(params), role=user.role,
    )
    return [{"id": str(r["id"]), "titre": r["titre"], "description": r.get("description"), "evenement_id": str(r["evenement_id"]) if r.get("evenement_id") else None} for r in rows]


@router.get("/membres/me/consultations/{consultation_id}")
def ma_consultation(consultation_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Detail (questions) of a consultation addressed to the member."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lie a un membre")
    axes = _membre_axes(user.membre_id, user.role)
    where, params = _visibilite_predicate(axes)
    cons = db.fetch_one(
        f"SELECT c.id, c.titre, c.description, c.statut FROM consultation c WHERE c.id = %s AND {where}",
        (consultation_id, *params), role=user.role,
    )
    if not cons:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="consultation introuvable")
    questions = db.fetch_all(
        "SELECT id, libelle, type, options, ordre FROM consultation_question WHERE consultation_id = %s ORDER BY ordre ASC",
        (consultation_id,), role=user.role,
    )
    return {
        "id": str(cons["id"]), "titre": cons["titre"], "description": cons.get("description"), "statut": cons["statut"],
        "questions": [{"id": str(q["id"]), "libelle": q["libelle"], "type": q["type"], "options": q.get("options") or []} for q in questions],
    }


@router.post("/membres/me/consultations/{consultation_id}/reponses", status_code=status.HTTP_201_CREATED)
def repondre(consultation_id: str, payload: ReponsesIn, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Submit answers once. Anti-doublon: one answer per (question, member), any
    entry point, enforced by a unique constraint (upsert on re-submission)."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lie a un membre")
    axes = _membre_axes(user.membre_id, user.role)
    where, params = _visibilite_predicate(axes)
    cons = db.fetch_one(
        f"SELECT c.id FROM consultation c WHERE c.id = %s AND c.statut = 'ouverte' AND {where}",
        (consultation_id, *params), role=user.role,
    )
    if not cons:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="consultation non disponible")
    valides = {str(q["id"]) for q in db.fetch_all("SELECT id FROM consultation_question WHERE consultation_id = %s", (consultation_id,), role=user.role)}
    enregistrees = 0
    for item in payload.reponses:
        if item.question_id not in valides:
            continue
        db.execute(
            "INSERT INTO consultation_reponse (consultation_id, question_id, membre_id, valeur, canal) "
            "VALUES (%s, %s, %s, %s, 'interne') "
            "ON CONFLICT (question_id, membre_id) DO UPDATE SET valeur = EXCLUDED.valeur, cree_le = now()",
            (consultation_id, item.question_id, user.membre_id, item.valeur),
            role=user.role,
        )
        enregistrees += 1
    return {"ok": True, "enregistrees": enregistrees}
