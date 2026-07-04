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
from .schemas import StatistiquesOut, UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["analytics"])

STAFF = ("super_admin", "admin", "gestionnaire", "controleur", "direction")
require_staff = require_roles(*STAFF)


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
               (SELECT count(*) FROM commission WHERE type_organisation = 'commission') AS commissions,
               (SELECT count(*) FROM commission WHERE type_organisation = 'mission') AS missions,
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
        missions_total=int(s.get("missions", 0)),
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
