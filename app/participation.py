"""Member participation per activity and its statistics.

A member's participation in an activity is a single row (unique per member and
event), so a person is never double counted. A QR check-in is the source of
truth: it marks the member present (source=scan) and locks that presence, so a
scanned member cannot re-declare their presence (no double count) but can still
leave an opinion and a rating. An online or non-scanned member declares present,
partial ("suivi partiel") or absent, editable until they validate; only a
validated declaration (or a scan) is counted.

The statistics cover every angle the administration needs: presence, partial,
absent, non-respondents, response/presence/participation rates, in-person vs
online split, ratings, and cross-breakdowns by gender, age, commission,
intendance, coordination, tribu, country and member type, plus global trends and
per-member attendance.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db
from .auth import current_user
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["participation"])

require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")

_STATUTS = ("present", "partiel", "absent")

# A participation is counted when it is a scan or a validated declaration.
_COMPTE = "(source = 'scan' OR valide)"


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


# --- Member: declare / view participation -----------------------------------

class ParticipationIn(BaseModel):
    statut: str | None = None  # present | partiel | absent
    avis: str | None = None
    note: int | None = None
    valider: bool = False


@router.get("/membres/me/evenements/{evenement_id}/participation")
def ma_participation(evenement_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    membre_id, role = ctx
    ev = db.fetch_one("SELECT id FROM evenement WHERE id = %s", (evenement_id,), role=role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    row = db.fetch_one(
        "SELECT statut, source, valide, avis, note FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
        role=role,
    )
    if not row:
        return {"statut": None, "source": None, "valide": False, "avis": None, "note": None, "deja_scanne": False, "verrouille": False}
    scanne = row["source"] == "scan"
    # A scanned presence, or an already-validated declaration, locks the status.
    verrouille = scanne or bool(row["valide"])
    return {
        "statut": row["statut"],
        "source": row["source"],
        "valide": bool(row["valide"]),
        "avis": row["avis"],
        "note": row["note"],
        "deja_scanne": scanne,
        "verrouille": verrouille,
    }


@router.put("/membres/me/evenements/{evenement_id}/participation")
def declarer_participation(evenement_id: str, payload: ParticipationIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Declare or update participation.

    Guardrails: a scanned member is already present and cannot change their
    status (only opinion/rating). A validated declaration is locked too. Only one
    row exists per member and event, so no double counting is possible.
    """
    membre_id, role = ctx
    if payload.note is not None and not 1 <= payload.note <= 5:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="note must be between 1 and 5")
    existing = db.fetch_one(
        "SELECT statut, source, valide FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
        role=role,
    )
    locked = bool(existing) and (existing["source"] == "scan" or existing["valide"])

    if locked:
        # Status is fixed; only feedback can be updated.
        db.execute(
            "UPDATE participation SET avis = COALESCE(%s, avis), note = COALESCE(%s, note), maj_le = now() WHERE evenement_id = %s AND membre_id = %s",
            (payload.avis, payload.note, evenement_id, membre_id),
            role=role,
        )
        return {"ok": True, "verrouille": True, "statut": existing["statut"], "message": "Votre présence est déjà enregistrée."}

    statut = payload.statut or (existing["statut"] if existing else None)
    if statut not in _STATUTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut must be present, partiel or absent")
    db.execute(
        """
        INSERT INTO participation (evenement_id, membre_id, statut, source, valide, avis, note)
        VALUES (%s, %s, %s, 'declaration', %s, %s, %s)
        ON CONFLICT (evenement_id, membre_id)
        DO UPDATE SET statut = EXCLUDED.statut, valide = EXCLUDED.valide,
            avis = COALESCE(EXCLUDED.avis, participation.avis),
            note = COALESCE(EXCLUDED.note, participation.note), maj_le = now()
        """,
        (evenement_id, membre_id, statut, payload.valider, payload.avis, payload.note),
        role=role,
    )
    return {"ok": True, "verrouille": payload.valider, "statut": statut, "valide": payload.valider}


# --- Admin: per-event statistics --------------------------------------------

_AGE_EXPR = (
    "CASE WHEN m.date_naissance IS NULL THEN 'Non renseigne' "
    "WHEN extract(year FROM age(m.date_naissance)) < 18 THEN 'moins de 18' "
    "WHEN extract(year FROM age(m.date_naissance)) < 26 THEN '18-25' "
    "WHEN extract(year FROM age(m.date_naissance)) < 36 THEN '26-35' "
    "WHEN extract(year FROM age(m.date_naissance)) < 51 THEN '36-50' "
    "ELSE '51 et plus' END"
)

_DIMENSIONS = {
    "genre": "coalesce(m.genre, 'Non renseigne')",
    "tranche_age": _AGE_EXPR,
    "commission": "coalesce(c.nom, 'Sans commission')",
    "intendance": "coalesce(i.nom, 'Sans intendance')",
    "coordination": "coalesce(co.nom, 'Sans coordination')",
    "tribu": "coalesce(t.nom, 'Sans tribu')",
    "pays": "coalesce(m.pays, 'Non renseigne')",
    "region": "coalesce(m.region, 'Non renseigne')",
    "type_membre": "coalesce(m.type_membre, 'Non renseigne')",
    "cheminement": "coalesce(m.cheminement_pastoral, 'Non renseigne')",
}

_JOINS = (
    "JOIN membre m ON m.id = p.membre_id "
    "LEFT JOIN commission c ON c.id = m.commission_id "
    "LEFT JOIN intendance i ON i.id = m.intendance_id "
    "LEFT JOIN coordination co ON co.id = i.coordination_id "
    "LEFT JOIN tribu t ON t.id = m.tribu_id"
)


def _breakdown(evenement_id: str, expr: str, role: str) -> list[dict[str, object]]:
    rows = db.fetch_all(
        f"""
        SELECT {expr} AS cle,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present') AS presents,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'partiel') AS partiels,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'absent') AS absents
        FROM participation p {_JOINS}
        WHERE p.evenement_id = %s
        GROUP BY {expr}
        ORDER BY presents DESC, cle ASC
        """,
        (evenement_id,),
        role=role,
    )
    return [{"cle": r["cle"], "presents": int(r["presents"]), "partiels": int(r["partiels"]), "absents": int(r["absents"])} for r in rows]


@router.get("/admin/evenements/{evenement_id}/participation-stats")
def participation_stats(evenement_id: str, user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, object]:
    ev = db.fetch_one("SELECT id, titre, debut, volet FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    agg = db.fetch_one(
        f"""
        SELECT
          count(*) FILTER (WHERE {_COMPTE}) AS repondants,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present') AS presents,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source = 'scan') AS presents_presentiel,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source = 'declaration') AS presents_enligne,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'partiel') AS partiels,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'absent') AS absents,
          count(*) FILTER (WHERE NOT {_COMPTE}) AS brouillons,
          round(avg(note) FILTER (WHERE note IS NOT NULL), 2) AS note_moyenne,
          count(note) AS nb_notes
        FROM participation WHERE evenement_id = %s
        """,
        (evenement_id,),
        role=user.role,
    ) or {}
    attendus = db.fetch_one("SELECT count(*) AS n FROM membre WHERE statut = 'actif'", (), role=user.role)
    total_attendus = int(attendus["n"]) if attendus else 0
    presents = int(agg.get("presents") or 0)
    presentiel = int(agg.get("presents_presentiel") or 0)
    enligne = int(agg.get("presents_enligne") or 0)
    partiels = int(agg.get("partiels") or 0)
    absents = int(agg.get("absents") or 0)
    repondants = int(agg.get("repondants") or 0)
    brouillons = int(agg.get("brouillons") or 0)
    non_repondants = max(0, total_attendus - repondants - brouillons)
    notes_dist = db.fetch_all(
        "SELECT note, count(*) AS n FROM participation WHERE evenement_id = %s AND note IS NOT NULL GROUP BY note ORDER BY note",
        (evenement_id,),
        role=user.role,
    )

    def taux(n: int, base: int) -> float:
        return round(100.0 * n / base, 1) if base else 0.0

    return {
        "evenement": {"id": str(ev["id"]), "titre": ev["titre"], "debut": ev["debut"].isoformat() if ev["debut"] else None, "volet": ev["volet"]},
        "effectif_attendu": total_attendus,
        "repondants": repondants,
        "non_repondants": non_repondants,
        "presents": presents,
        "presents_presentiel": presentiel,
        "presents_enligne": enligne,
        "partiels": partiels,
        "absents": absents,
        "brouillons": brouillons,
        "taux_reponse": taux(repondants, total_attendus),
        "taux_non_reponse": taux(non_repondants, total_attendus),
        "taux_presence": taux(presents, total_attendus),
        "taux_presence_repondants": taux(presents, repondants),
        "taux_participation": taux(presents + partiels, total_attendus),
        "taux_partiel": taux(partiels, total_attendus),
        "taux_absence": taux(absents, total_attendus),
        "part_presentiel": taux(presentiel, presents),
        "part_en_ligne": taux(enligne, presents),
        "note_moyenne": float(agg["note_moyenne"]) if agg.get("note_moyenne") is not None else None,
        "nb_notes": int(agg.get("nb_notes") or 0),
        "taux_reponse_note": taux(int(agg.get("nb_notes") or 0), presents),
        "distribution_notes": [{"note": int(r["note"]), "n": int(r["n"])} for r in notes_dist],
        "repartitions": {dim: _breakdown(evenement_id, expr, user.role) for dim, expr in _DIMENSIONS.items()},
    }


# --- Admin: global participation trends -------------------------------------

@router.get("/admin/participation/global")
def participation_global(user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, object]:
    role = user.role
    # Presence rate per event, most recent first (time series).
    serie = db.fetch_all(
        f"""
        SELECT e.id, e.titre, e.debut, e.volet,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present') AS presents,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'partiel') AS partiels,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'absent') AS absents
        FROM evenement e LEFT JOIN participation p ON p.evenement_id = e.id
        GROUP BY e.id, e.titre, e.debut, e.volet
        ORDER BY e.debut DESC NULLS LAST
        LIMIT 30
        """,
        (),
        role=role,
    )
    # Global split.
    split = db.fetch_one(
        f"""
        SELECT count(*) FILTER (WHERE {_COMPTE} AND statut = 'present') AS presents,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'partiel') AS partiels,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'absent') AS absents,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source = 'scan') AS presentiel,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source = 'declaration') AS en_ligne
        FROM participation
        """,
        (),
        role=role,
    ) or {}
    nb_evenements = db.fetch_one("SELECT count(*) AS n FROM evenement", (), role=role)
    total_ev = int(nb_evenements["n"]) if nb_evenements else 0
    # Attendance per member (assiduity): present count / number of events.
    assidus = db.fetch_all(
        f"""
        SELECT trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre, m.matricule,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present') AS presents
        FROM membre m LEFT JOIN participation p ON p.membre_id = m.id
        WHERE m.statut = 'actif'
        GROUP BY m.id, m.prenoms, m.nom, m.matricule
        ORDER BY presents DESC
        LIMIT 10
        """,
        (),
        role=role,
    )
    a_relancer = db.fetch_all(
        f"""
        SELECT trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre, m.matricule,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present') AS presents
        FROM membre m LEFT JOIN participation p ON p.membre_id = m.id
        WHERE m.statut = 'actif'
        GROUP BY m.id, m.prenoms, m.nom, m.matricule
        ORDER BY presents ASC, membre ASC
        LIMIT 10
        """,
        (),
        role=role,
    )
    return {
        "nb_evenements": total_ev,
        "repartition_globale": {
            "presents": int(split.get("presents") or 0),
            "partiels": int(split.get("partiels") or 0),
            "absents": int(split.get("absents") or 0),
            "presentiel": int(split.get("presentiel") or 0),
            "en_ligne": int(split.get("en_ligne") or 0),
        },
        "serie_evenements": [
            {"id": str(r["id"]), "titre": r["titre"], "debut": r["debut"].isoformat() if r["debut"] else None, "volet": r["volet"],
             "presents": int(r["presents"]), "partiels": int(r["partiels"]), "absents": int(r["absents"])}
            for r in serie
        ],
        "top_assidus": [{"membre": r["membre"], "matricule": r["matricule"], "presents": int(r["presents"])} for r in assidus],
        "a_relancer": [{"membre": r["membre"], "matricule": r["matricule"], "presents": int(r["presents"])} for r in a_relancer],
    }
