"""Who answers for which tribe, since when, and appointed by whom.

A tribe has a patriarche, and the patriarche must belong to the tribe. Supervision is
a different relation: whoever answers for a tribe before the governance is often not
one of its members, and one person can carry several tribes. Nothing recorded that,
so the question had no answer anywhere.

A supervision is never overwritten. Replacing one closes the previous period and
opens a new one, because "who supervised this tribe last quarter" is a question an
organisation has to be able to answer, and an overwrite destroys it.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["organisation"])

_ROLES = ("superviseur", "referent", "accompagnateur")


def _porteur(r: dict[str, Any]) -> dict[str, Any]:
    """Who carries the supervision, as one readable block whichever kind it is."""
    if r.get("membre_id"):
        nom = f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip()
        return {"type": "membre", "id": str(r["membre_id"]),
                "libelle": nom or str(r.get("email") or "membre"),
                "matricule": r.get("matricule")}
    return {"type": "equipe", "id": str(r["equipe_speciale_id"]),
            "libelle": r.get("equipe_nom") or "équipe spéciale", "matricule": None}


_SELECT = (
    "SELECT s.id, s.tribu_id, t.nom AS tribu_nom, s.membre_id, s.equipe_speciale_id, "
    "s.role_supervision, s.debut, s.fin, s.motif, "
    "m.prenoms, m.nom, m.email, m.matricule, e.nom AS equipe_nom, "
    "u.email AS attribue_par_email "
    "FROM tribu_supervision s "
    "JOIN tribu t ON t.id = s.tribu_id "
    "LEFT JOIN membre m ON m.id = s.membre_id "
    "LEFT JOIN equipe_speciale e ON e.id = s.equipe_speciale_id "
    "LEFT JOIN utilisateur u ON u.id = s.attribue_par "
)


def _rendu(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "tribu": {"id": str(r["tribu_id"]), "nom": r.get("tribu_nom")},
        "porteur": _porteur(r),
        "role": r.get("role_supervision"),
        "debut": r.get("debut"),
        "fin": r.get("fin"),
        "en_cours": r.get("fin") is None,
        "motif": r.get("motif"),
        "attribue_par": r.get("attribue_par_email"),
    }


@router.get("/tribus/supervisions")
def lister_supervisions(
    user: Annotated[UserMe, Depends(require_permission("organisation.supervision"))],
    tribu_id: str | None = Query(default=None),
    inclure_closes: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Current supervisions, and the closed ones when asked for."""
    conditions: list[str] = []
    params: list[Any] = []
    if not inclure_closes:
        conditions.append("s.fin IS NULL")
    if tribu_id:
        conditions.append("s.tribu_id = %s")
        params.append(tribu_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM tribu_supervision s {where}", tuple(params), role=user.role,
    ) or {}).get("n") or 0)
    lignes = db.fetch_all(
        f"{_SELECT} {where} ORDER BY s.fin IS NOT NULL, t.nom, s.debut DESC LIMIT %s OFFSET %s",
        (*params, taille, (page - 1) * taille), role=user.role,
    )
    # Tribes with nobody answering for them: the gap is the reason this screen exists,
    # so it is reported rather than left for someone to notice by scrolling.
    sans = db.fetch_all(
        "SELECT t.id, t.nom FROM tribu t "
        "WHERE NOT EXISTS (SELECT 1 FROM tribu_supervision s "
        "                   WHERE s.tribu_id = t.id AND s.fin IS NULL) "
        "ORDER BY t.nom",
        (), role=user.role,
    )
    return {
        "items": [_rendu(r) for r in lignes],
        "total": total, "page": page, "taille": taille,
        "pages": max(1, (total + taille - 1) // taille),
        "tribus_sans_supervision": [{"id": str(t["id"]), "nom": t["nom"]} for t in sans],
    }


class SupervisionIn(BaseModel):
    tribu_id: str = Field(min_length=1)
    membre_id: str | None = None
    equipe_speciale_id: str | None = None
    role: str = Field(default="superviseur")
    motif: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _un_seul_porteur(self) -> SupervisionIn:
        # Refused here rather than left to the check constraint, so the caller reads a
        # sentence instead of a database error.
        if bool(self.membre_id) == bool(self.equipe_speciale_id):
            raise ValueError("désignez soit un membre, soit une équipe spéciale, pas les deux")
        return self


@router.post("/tribus/supervisions", status_code=status.HTTP_201_CREATED)
def designer(
    payload: SupervisionIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("organisation.supervision"))],
) -> dict[str, Any]:
    """Appoint somebody to answer for a tribe.

    A tribe can have several supervisions open at once (an individual and a body, or
    two complementary roles), so this does not close what is already there. Closing is
    an explicit act, because ending someone's responsibility should never be a silent
    side effect of appointing someone else.
    """
    if payload.role not in _ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"rôle inconnu, attendu : {', '.join(_ROLES)}")
    if not db.fetch_one("SELECT id FROM tribu WHERE id = %s", (payload.tribu_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tribu inconnue")
    if payload.membre_id and not db.fetch_one(
        "SELECT id FROM membre WHERE id = %s AND statut = 'actif'", (payload.membre_id,), role=user.role,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre inconnu ou inactif")
    if payload.equipe_speciale_id and not db.fetch_one(
        "SELECT id FROM equipe_speciale WHERE id = %s AND active = true",
        (payload.equipe_speciale_id,), role=user.role,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="équipe spéciale inconnue ou inactive")

    existante = db.fetch_one(
        "SELECT id FROM tribu_supervision WHERE tribu_id = %s AND fin IS NULL "
        "AND membre_id IS NOT DISTINCT FROM %s AND equipe_speciale_id IS NOT DISTINCT FROM %s",
        (payload.tribu_id, payload.membre_id, payload.equipe_speciale_id), role=user.role,
    )
    if existante:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="cette supervision est déjà en cours pour cette tribu")

    ligne = db.execute(
        "INSERT INTO tribu_supervision (tribu_id, membre_id, equipe_speciale_id, role_supervision, attribue_par, motif) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (payload.tribu_id, payload.membre_id, payload.equipe_speciale_id,
         payload.role, user.id, payload.motif), role=user.role,
    )
    sid = str(ligne["id"]) if ligne else None
    audit.log(user.id, user.role, "supervision_tribu_designee", "tribu", payload.tribu_id,
              {"supervision_id": sid, "membre_id": payload.membre_id,
               "equipe_speciale_id": payload.equipe_speciale_id, "role": payload.role})
    return {"id": sid, "tribu_id": payload.tribu_id, "en_cours": True}


class ClotureIn(BaseModel):
    motif: str | None = Field(default=None, max_length=500)


@router.post("/tribus/supervisions/{supervision_id}/clore")
def clore(
    supervision_id: str,
    payload: ClotureIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("organisation.supervision"))],
) -> dict[str, Any]:
    """End a supervision, keeping the period it covered.

    The row is closed, never deleted: erasing it would erase the answer to who was
    responsible at the time, which is exactly what an audit asks for.
    """
    ligne = db.execute(
        "UPDATE tribu_supervision SET fin = now(), "
        "motif = COALESCE(NULLIF(%s, ''), motif) "
        "WHERE id = %s AND fin IS NULL RETURNING id, tribu_id",
        (payload.motif or "", supervision_id), role=user.role,
    )
    if not ligne:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="supervision introuvable ou déjà close")
    audit.log(user.id, user.role, "supervision_tribu_close", "tribu", str(ligne["tribu_id"]),
              {"supervision_id": supervision_id, "motif": payload.motif})
    return {"id": supervision_id, "en_cours": False}


@router.get("/membres/{membre_id}/supervisions")
def supervisions_du_membre(
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("organisation.supervision"))],
) -> dict[str, Any]:
    """Every tribe this member answers for, including through a team they sit on."""
    directes = db.fetch_all(
        f"{_SELECT} WHERE s.membre_id = %s ORDER BY s.fin IS NOT NULL, s.debut DESC",
        (membre_id,), role=user.role,
    )
    par_equipe = db.fetch_all(
        f"{_SELECT} JOIN membre_equipe_speciale me ON me.equipe_id = s.equipe_speciale_id "
        "WHERE me.membre_id = %s AND me.actif = true AND me.fin IS NULL AND s.fin IS NULL "
        "ORDER BY t.nom",
        (membre_id,), role=user.role,
    )
    return {
        "membre_id": membre_id,
        "directes": [_rendu(r) for r in directes],
        "par_equipe": [_rendu(r) for r in par_equipe],
        "total_en_cours": sum(1 for r in directes if r.get("fin") is None) + len(par_equipe),
    }
