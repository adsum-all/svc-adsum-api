"""Analytics endpoints: aggregated statistics and duplicate member detection.

Duplicate detection helps the administration catch a member registered twice
(by phone or by name), which is not allowed, and flag possible infiltration.
Reserved to staff; the figures are read under the caller RLS role (ADR-0002).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import db
from .membre_filters import exclusion_technique
from .permissions_rbac import require_permission
from .schemas import StatistiquesOut, UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["analytics"])



@router.get("/statistiques", response_model=StatistiquesOut)
def statistiques(user: Annotated[UserMe, Depends(require_permission("statistiques.consulter"))]) -> StatistiquesOut:
    role = user.role
    membres = db.fetch_one(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE statut = 'actif') AS actifs,
               count(*) FILTER (WHERE verifie) AS verifies,
               count(*) FILTER (WHERE NOT verifie) AS en_attente
        FROM membre
        WHERE """ + exclusion_technique("membre") + """
        """,
        (),
        role=role,
    )
    scalar = db.fetch_one(
        """
        SELECT (SELECT count(*) FROM evenement) AS evenements,
               (SELECT count(*) FROM commission WHERE type_organisation = 'commission') AS commissions,
               (SELECT count(*) FROM commission WHERE type_organisation = 'mission') AS missions,
               (SELECT count(*) FROM intendance) AS intendances
        """,
        (),
        role=role,
    )
    # Cumulative attendance from the single canonical layer (deduplicated across the
    # presence and participation tables), so this total matches the back-office and
    # never diverges from the participation screens.
    from . import stats_core

    presences_total = stats_core.presences_cumulees(role)
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
        WHERE """ + exclusion_technique("membre") + """
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
        WHERE NOT verifie AND """ + exclusion_technique("membre") + """
        ORDER BY cree_le DESC
        LIMIT 8
        """,
        (),
        role=role,
    )
    # Distribution of activities across the administrable event types. Every published
    # type is listed (even at 0), plus a synthetic bucket for activities without a type,
    # so the dashboard shows the full split and its share.
    par_type = db.fetch_all(
        """
        SELECT te.nom AS nom, te.couleur AS couleur, count(e.id) AS total, te.ordre AS ordre
        FROM type_evenement te
        LEFT JOIN evenement e ON e.type_evenement_id = te.id
        WHERE te.publie
        GROUP BY te.id, te.nom, te.couleur, te.ordre
        UNION ALL
        SELECT 'Sans type' AS nom, '#94A3B8' AS couleur, count(*) AS total, 100000 AS ordre
        FROM evenement WHERE type_evenement_id IS NULL
        ORDER BY total DESC, ordre ASC
        """,
        (),
        role=role,
    )
    total_evts = sum(int(r["total"]) for r in par_type) or 1
    m = membres or {}
    s = scalar or {}
    return StatistiquesOut(
        membres_total=int(m.get("total", 0)),
        membres_actifs=int(m.get("actifs", 0)),
        membres_verifies=int(m.get("verifies", 0)),
        membres_en_attente=int(m.get("en_attente", 0)),
        evenements_total=int(s.get("evenements", 0)),
        presences_total=presences_total,
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
        par_type_evenement=[
            {
                "nom": r["nom"],
                "couleur": r["couleur"],
                "total": int(r["total"]),
                "pourcentage": round(int(r["total"]) * 100 / total_evts, 1),
            }
            for r in par_type
        ],
    )
