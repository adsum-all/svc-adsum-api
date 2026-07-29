"""Turn "I belong to the leading team" into something somebody can act on.

The registration form asks the question, the answer is written on the member and read
back in their file, and nothing ever happened next. A member declared themselves and
waited; no screen listed the declarations, and no act turned one into a membership.
The flag was a dead end.

This module is the missing step: the declarations form a queue, each one is confirmed
or refused by a named person on a dated day, and confirming it creates the membership
of the leading team rather than leaving the two to disagree forever.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["organisation"])

# The team a confirmed declaration joins. Looked up by code so renaming the team in
# the interface never breaks the link.
_CODE_EQUIPE = "equipe_dirigeante"


@router.get("/equipe-dirigeante/declarations")
def lister(
    user: Annotated[UserMe, Depends(require_permission("organisation.equipes"))],
    statut: str = Query(default="a_traiter"),
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """The declarations, newest first, with what the member's file already says."""
    if statut not in ("a_traiter", "confirmee", "refusee", "toutes"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut inconnu")
    where = "" if statut == "toutes" else "WHERE d.statut = %s"
    params: tuple[Any, ...] = () if statut == "toutes" else (statut,)

    total = int((db.fetch_one(
        f"SELECT COUNT(*) AS n FROM declaration_equipe_dirigeante d {where}", params, role=user.role,
    ) or {}).get("n") or 0)
    lignes = db.fetch_all(
        "SELECT d.id, d.membre_id, d.declaree_le, d.statut, d.traitee_le, d.commentaire, "
        "m.prenoms, m.nom, m.email, m.matricule, m.statut_inscription, "
        "u.email AS traitee_par_email, "
        "EXISTS (SELECT 1 FROM membre_equipe_speciale me "
        "         JOIN equipe_speciale e ON e.id = me.equipe_id "
        "        WHERE me.membre_id = m.id AND me.actif = true AND me.fin IS NULL "
        f"          AND e.code = '{_CODE_EQUIPE}') AS deja_membre "
        "FROM declaration_equipe_dirigeante d "
        "JOIN membre m ON m.id = d.membre_id "
        "LEFT JOIN utilisateur u ON u.id = d.traitee_par "
        f"{where} ORDER BY d.declaree_le DESC LIMIT %s OFFSET %s",
        (*params, taille, (page - 1) * taille), role=user.role,
    )
    items = [
        {
            "id": str(r["id"]),
            "membre_id": str(r["membre_id"]),
            "nom": f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or None,
            "email": r.get("email"),
            "matricule": r.get("matricule"),
            "statut_inscription": r.get("statut_inscription"),
            "declaree_le": r.get("declaree_le"),
            "statut": r.get("statut"),
            "traitee_le": r.get("traitee_le"),
            "traitee_par": r.get("traitee_par_email"),
            "commentaire": r.get("commentaire"),
            # A declaration whose member already sits on the team needs no act: saying
            # so avoids an administrator confirming what is already true.
            "deja_membre": bool(r.get("deja_membre")),
        }
        for r in lignes
    ]
    attente = int((db.fetch_one(
        "SELECT COUNT(*) AS n FROM declaration_equipe_dirigeante WHERE statut = 'a_traiter'",
        (), role=user.role,
    ) or {}).get("n") or 0)
    return {"items": items, "total": total, "page": page, "taille": taille,
            "pages": max(1, (total + taille - 1) // taille), "en_attente": attente}


class DecisionIn(BaseModel):
    confirmee: bool
    commentaire: str | None = Field(default=None, max_length=500)


@router.post("/equipe-dirigeante/declarations/{declaration_id}")
def decider(
    declaration_id: str,
    payload: DecisionIn,
    user: Annotated[UserMe, Depends(require_permission_ecriture("organisation.equipes"))],
) -> dict[str, Any]:
    """Confirm or refuse one declaration, and make the membership match the decision.

    Both happen in one transaction: a confirmation that recorded a decision without
    creating the membership would leave the file saying yes and the team saying no,
    which is the disagreement this module exists to end.
    """
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT d.id, d.membre_id, d.statut FROM declaration_equipe_dirigeante d "
            "WHERE d.id = %s FOR UPDATE",
            (declaration_id,),
        )
        ligne = cur.fetchone()
        if not ligne:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="déclaration inconnue")
        if str(ligne["statut"]) != "a_traiter":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="cette déclaration a déjà été traitée")
        membre_id = str(ligne["membre_id"])

        cur.execute("SELECT id FROM equipe_speciale WHERE code = %s AND active = true", (_CODE_EQUIPE,))
        equipe = cur.fetchone()
        if payload.confirmee and not equipe:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="l'équipe dirigeante n'existe pas ou est inactive, créez-la avant de confirmer",
            )

        cur.execute(
            "UPDATE declaration_equipe_dirigeante SET statut = %s, traitee_le = now(), "
            "traitee_par = %s, commentaire = %s WHERE id = %s",
            ("confirmee" if payload.confirmee else "refusee", user.id, payload.commentaire, declaration_id),
        )
        if payload.confirmee and equipe:
            # Reopen a CLOSED membership rather than inserting a twin: the row may
            # already exist from a previous period, and a plain insert would collide.
            # An already-open one is left exactly as it is: overwriting its start
            # date, who granted it and why would erase the record of a membership
            # that has been running, and replace it with today's decision, which is
            # the opposite of what the traceability columns exist for.
            cur.execute(
                "UPDATE membre_equipe_speciale SET actif = true, fin = NULL, debut = now(), "
                "attribue_par = %s, motif = %s "
                "WHERE membre_id = %s AND equipe_id = %s "
                "  AND (actif = false OR fin IS NOT NULL) RETURNING id",
                (user.id, payload.commentaire, membre_id, str(equipe["id"])),
            )
            if not cur.fetchone():
                cur.execute(
                    "SELECT id FROM membre_equipe_speciale "
                    "WHERE membre_id = %s AND equipe_id = %s AND actif = true AND fin IS NULL",
                    (membre_id, str(equipe["id"])),
                )
                deja_ouverte = cur.fetchone() is not None
            else:
                deja_ouverte = True
            if not deja_ouverte:
                cur.execute(
                    "INSERT INTO membre_equipe_speciale (membre_id, equipe_id, role, actif, attribue_par, motif) "
                    "VALUES (%s, %s, 'membre', true, %s, %s)",
                    (membre_id, str(equipe["id"]), user.id, payload.commentaire),
                )
        elif not payload.confirmee:
            # A refusal clears the declared flag, so the member's file stops asserting
            # something the governance has decided is not the case.
            cur.execute(
                "UPDATE membre SET equipe_dirigeante_declaree = false WHERE id = %s", (membre_id,)
            )

    audit.log(user.id, user.role,
              "declaration_equipe_dirigeante_confirmee" if payload.confirmee else "declaration_equipe_dirigeante_refusee",
              "membre", membre_id, {"declaration_id": declaration_id, "commentaire": payload.commentaire})
    return {"id": declaration_id, "statut": "confirmee" if payload.confirmee else "refusee",
            "membre_id": membre_id}
