"""Analytics endpoints: aggregated statistics and duplicate member detection.

Duplicate detection helps the administration catch a member registered twice
(by phone or by name), which is not allowed, and flag possible infiltration.
Reserved to staff; the figures are read under the caller RLS role (ADR-0002).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import db
from .deps import require_roles
from .mappers import MEMBRE_PROFILE_FROM, MEMBRE_PROFILE_SELECT, membre_row_to_profile
from .schemas import DoublonGroupe, StatistiquesOut, UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["analytics"])

STAFF = ("super_admin", "admin", "gestionnaire", "controleur", "direction")
require_staff = require_roles(*STAFF)

_PHONE_EXPR = "regexp_replace(coalesce(m.telephone, ''), '[^0-9]', '', 'g')"
_NAME_EXPR = "lower(trim(coalesce(m.prenoms, '') || ' ' || coalesce(m.nom, '')))"


def _duplicate_groups(role: str, expr: str, critere: str) -> list[DoublonGroupe]:
    rows = db.fetch_all(
        f"""
        SELECT {expr} AS valeur, count(*) AS total
        FROM membre m
        WHERE length({expr}) > 3
        GROUP BY {expr}
        HAVING count(*) > 1
        ORDER BY count(*) DESC
        LIMIT 50
        """,
        (),
        role=role,
    )
    groups: list[DoublonGroupe] = []
    for r in rows:
        members = db.fetch_all(
            f"SELECT {MEMBRE_PROFILE_SELECT} {MEMBRE_PROFILE_FROM} WHERE {expr} = %s ORDER BY m.cree_le ASC",
            (r["valeur"],),
            role=role,
        )
        groups.append(
            DoublonGroupe(
                critere=critere,
                valeur=str(r["valeur"]),
                membres=[membre_row_to_profile(m) for m in members],
            )
        )
    return groups


@router.get("/doublons", response_model=list[DoublonGroupe])
def doublons(user: Annotated[UserMe, Depends(require_staff)]) -> list[DoublonGroupe]:
    """Potential duplicate members, grouped by identical phone or identical name."""
    by_phone = _duplicate_groups(user.role, _PHONE_EXPR, "telephone")
    by_name = _duplicate_groups(user.role, _NAME_EXPR, "nom")
    return by_phone + by_name


@router.get("/statistiques", response_model=StatistiquesOut)
def statistiques(user: Annotated[UserMe, Depends(require_staff)]) -> StatistiquesOut:
    role = user.role
    membres = db.fetch_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE statut = 'actif') AS actifs,
               count(*) FILTER (WHERE verifie) AS verifies,
               count(*) FILTER (WHERE NOT verifie) AS en_attente
        FROM membre
        """,
        (),
        role=role,
    )
    scalar = db.fetch_one(
        """
        SELECT (SELECT count(*) FROM evenement) AS evenements,
               (SELECT count(*) FROM presence) AS presences,
               (SELECT count(*) FROM commission) AS commissions,
               (SELECT count(*) FROM intendance) AS intendances
        """,
        (),
        role=role,
    )
    par_commission = db.fetch_all(
        """
        SELECT c.nom AS commission, count(m.id) AS total
        FROM commission c
        LEFT JOIN membre m ON m.commission_id = c.id
        GROUP BY c.nom
        ORDER BY total DESC
        """,
        (),
        role=role,
    )
    par_cheminement = db.fetch_all(
        """
        SELECT cheminement_pastoral AS cheminement, count(*) AS total
        FROM membre
        GROUP BY cheminement_pastoral
        ORDER BY total DESC
        """,
        (),
        role=role,
    )
    entrees_mensuelles = db.fetch_all(
        """
        SELECT to_char(date_trunc('month', mois), 'YYYY-MM') AS mois,
               count(m.id) AS total
        FROM generate_series(
                 date_trunc('month', current_date) - interval '11 months',
                 date_trunc('month', current_date),
                 interval '1 month'
             ) AS mois
        LEFT JOIN membre m
               ON m.date_entree IS NOT NULL
              AND date_trunc('month', m.date_entree) = date_trunc('month', mois)
        GROUP BY mois
        ORDER BY mois ASC
        """,
        (),
        role=role,
    )
    a_verifier = db.fetch_all(
        """
        SELECT id, matricule, prenoms, nom
        FROM membre
        WHERE NOT verifie
        ORDER BY cree_le DESC
        LIMIT 8
        """,
        (),
        role=role,
    )
    m = membres or {}
    s = scalar or {}
    return StatistiquesOut(
        membres_total=int(m.get("total", 0)),
        membres_actifs=int(m.get("actifs", 0)),
        membres_verifies=int(m.get("verifies", 0)),
        membres_en_attente=int(m.get("en_attente", 0)),
        evenements_total=int(s.get("evenements", 0)),
        presences_total=int(s.get("presences", 0)),
        commissions_total=int(s.get("commissions", 0)),
        intendances_total=int(s.get("intendances", 0)),
        par_commission=[{"commission": r["commission"], "total": int(r["total"])} for r in par_commission],
        par_cheminement=[
            {"cheminement": r["cheminement"], "total": int(r["total"])} for r in par_cheminement
        ],
        entrees_mensuelles=[
            {"mois": r["mois"], "total": int(r["total"])} for r in entrees_mensuelles
        ],
        membres_a_verifier=[
            {
                "id": str(r["id"]),
                "matricule": r["matricule"],
                "prenoms": r["prenoms"],
                "nom": r["nom"],
            }
            for r in a_verifier
        ],
    )
