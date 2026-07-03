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
_MODALITES = ("presentiel", "en_ligne")

# A participation is counted when it is a scan or a validated declaration.
_COMPTE = "(source = 'scan' OR valide)"

# Single source of truth for the response window: the real end of the session
# (or debut + 1 day when no end is set) plus the per-event duration when the
# administration set one, else the global admin parameter
# questionnaire_fenetre_heures, else 6 hours. Every read (display), write
# (submission guard) and reminder must use this same formula.
FENETRE_FIN_SQL = (
    "coalesce(e.fin, e.debut + interval '1 day') + make_interval(hours => coalesce("
    "e.fenetre_reponse_heures, "
    "(SELECT (p.valeur #>> '{}')::int FROM parametre p WHERE p.cle = 'questionnaire_fenetre_heures'), "
    "6))"
)


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


# --- Member: declare / view participation -----------------------------------

class ParticipationIn(BaseModel):
    statut: str | None = None  # present | partiel | absent
    modalite: str | None = None  # presentiel | en_ligne (declarative; ignored when scanned)
    avis: str | None = None
    note: int | None = None
    valider: bool = False


@router.get("/membres/me/evenements/{evenement_id}/participation")
def ma_participation(evenement_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    membre_id, role = ctx
    ev = db.fetch_one(
        f"SELECT e.debut, (e.debut IS NOT NULL AND now() >= e.debut) AS demarree, "
        f"(e.debut IS NOT NULL AND now() > {FENETRE_FIN_SQL}) AS cloture, "
        f"{FENETRE_FIN_SQL} AS cloture_le "
        "FROM evenement e WHERE e.id = %s",
        (evenement_id,),
        role=role,
    )
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    # The declaration form is only available once the activity has started, and
    # closes after the window: the screen must tell the same truth as the server
    # (a form shown after closure would silently fail on submit).
    ouvert = bool(ev["demarree"])
    cloture = bool(ev["cloture"])
    disponible_le = ev["debut"].isoformat() if ev["debut"] else None
    cloture_le = ev["cloture_le"].isoformat() if ev.get("cloture_le") else None
    row = db.fetch_one(
        "SELECT statut, source, valide, avis, note, modalite FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
        role=role,
    )
    if not row:
        return {"statut": None, "source": None, "valide": False, "avis": None, "note": None, "modalite": None, "deja_scanne": False, "verrouille": False, "ouvert": ouvert, "disponible_le": disponible_le, "cloture": cloture, "cloture_le": cloture_le}
    scanne = row["source"] == "scan"
    # Finalized (validated) participation is fully immutable. A scanned member is
    # present but may still give their feedback once (which finalizes it).
    verrouille = bool(row["valide"])
    return {
        "statut": row["statut"],
        "source": row["source"],
        "valide": bool(row["valide"]),
        "avis": row["avis"],
        "note": row["note"],
        "modalite": row.get("modalite"),
        "deja_scanne": scanne,
        "verrouille": verrouille,
        "ouvert": ouvert,
        "disponible_le": disponible_le,
        "cloture": cloture,
        "cloture_le": cloture_le,
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
    if payload.modalite is not None and payload.modalite not in _MODALITES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="modalite must be presentiel or en_ligne")
    # Attendance window, enforced server-side with the same formula the display
    # uses (per-event duration, else the admin parameter, else 6 hours). No one
    # can declare for a future event, nor once the window is over.
    ev = db.fetch_one(
        f"SELECT e.debut, (e.debut IS NOT NULL AND now() >= e.debut) AS demarree, "
        f"(e.debut IS NOT NULL AND now() > {FENETRE_FIN_SQL}) AS cloture "
        "FROM evenement e WHERE e.id = %s",
        (evenement_id,),
        role=role,
    )
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    existing = db.fetch_one(
        "SELECT statut, source, valide, modalite FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
        role=role,
    )

    # Once finalized (validated), participation is immutable: no re-participation,
    # no changing the rating or opinion afterwards. It is counted exactly once.
    if existing and existing["valide"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Votre participation est déjà enregistrée et ne peut plus être modifiée.")

    # The form only opens once the activity has started: no one can declare
    # participation to an event that has not happened yet.
    if not existing and not ev["demarree"]:
        quand = ev["debut"].strftime("%d/%m/%Y à %Hh%M") if ev["debut"] else ""
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"le formulaire sera disponible au début de l'activité ({quand})")
    # The form closes after the activity: a member who did not declare in time can
    # no longer create a record (an admin correction remains possible server-side).
    if not existing and ev["cloture"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Le formulaire de présence de cette activité est clôturé.")

    scanned = bool(existing) and existing["source"] == "scan"
    if scanned:
        # A scanned member is present on site; status and modality are strong
        # proof and cannot be edited. They may add feedback, and validating
        # finalizes it.
        new_statut, new_source, new_modalite = "present", "scan", "presentiel"
    else:
        new_statut = payload.statut or (existing["statut"] if existing else None)
        if new_statut not in _STATUTS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut must be present, partiel or absent")
        new_source = "declaration"
        # Declarative modality: asked only when there is no scan proof. It is
        # required to validate an attended/partial declaration, meaningless for
        # an absence.
        new_modalite = payload.modalite or (existing.get("modalite") if existing else None)
        if new_statut == "absent":
            new_modalite = None
        elif payload.valider and new_modalite is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Précisez comment vous avez suivi l'activité (présentiel ou en ligne).",
            )

    # Atomic upsert: a scan always wins (its presence and modality are never
    # downgraded by a concurrent declaration), and a finalized row is never
    # touched (WHERE NOT valide).
    db.execute(
        """
        INSERT INTO participation (evenement_id, membre_id, statut, source, valide, avis, note, modalite)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (evenement_id, membre_id)
        DO UPDATE SET
            statut = CASE WHEN participation.source = 'scan' THEN 'present' ELSE EXCLUDED.statut END,
            source = CASE WHEN participation.source = 'scan' THEN 'scan' ELSE EXCLUDED.source END,
            modalite = CASE WHEN participation.source = 'scan' THEN 'presentiel' ELSE EXCLUDED.modalite END,
            valide = EXCLUDED.valide,
            avis = COALESCE(EXCLUDED.avis, participation.avis),
            note = COALESCE(EXCLUDED.note, participation.note),
            maj_le = now()
        WHERE NOT participation.valide
        """,
        (evenement_id, membre_id, new_statut, new_source, payload.valider, payload.avis, payload.note, new_modalite),
        role=role,
    )
    return {"ok": True, "verrouille": payload.valider, "statut": new_statut, "valide": payload.valider, "modalite": new_modalite}


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
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source <> 'scan' AND modalite = 'presentiel') AS presents_presentiel_declare,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND modalite = 'en_ligne') AS presents_enligne,
          count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source <> 'scan' AND modalite IS NULL) AS presents_modalite_inconnue,
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
    # Modality x follow-up cross table, with the proof level made explicit:
    # a scan is strong on-site proof; everything else is declarative.
    croisement = db.fetch_all(
        f"""
        SELECT CASE WHEN source = 'scan' THEN 'presentiel_prouve'
                    WHEN modalite = 'presentiel' THEN 'presentiel_declare'
                    WHEN modalite = 'en_ligne' THEN 'en_ligne_declare'
                    ELSE 'modalite_inconnue' END AS modalite,
               statut, count(*) AS n
        FROM participation WHERE evenement_id = %s AND {_COMPTE}
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        (evenement_id,),
        role=user.role,
    )
    # Non-respondents split by whether they signed in during the response
    # window (weak connectivity signal, never counted as participation).
    nr = db.fetch_one(
        f"""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM utilisateur u JOIN session s ON s.utilisateur_id = u.id
                   WHERE u.membre_id = m.id AND s.cree_le BETWEEN e.debut AND {FENETRE_FIN_SQL}
               )) AS connectes
        FROM membre m, evenement e
        WHERE e.id = %s AND m.statut = 'actif'
          AND NOT EXISTS (
              SELECT 1 FROM participation p
              WHERE p.evenement_id = e.id AND p.membre_id = m.id AND (p.source = 'scan' OR p.valide)
          )
        """,
        (evenement_id,),
        role=user.role,
    ) or {}
    attendus = db.fetch_one("SELECT count(*) AS n FROM membre WHERE statut = 'actif'", (), role=user.role)
    total_attendus = int(attendus["n"]) if attendus else 0
    presents = int(agg.get("presents") or 0)
    presentiel = int(agg.get("presents_presentiel") or 0)
    presentiel_declare = int(agg.get("presents_presentiel_declare") or 0)
    enligne = int(agg.get("presents_enligne") or 0)
    modalite_inconnue = int(agg.get("presents_modalite_inconnue") or 0)
    partiels = int(agg.get("partiels") or 0)
    absents = int(agg.get("absents") or 0)
    repondants = int(agg.get("repondants") or 0)
    brouillons = int(agg.get("brouillons") or 0)
    non_repondants = max(0, total_attendus - repondants - brouillons)
    nr_connectes = int(nr.get("connectes") or 0)
    nr_total = int(nr.get("n") or 0)
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
        "presents_presentiel_declare": presentiel_declare,
        "presents_enligne": enligne,
        "presents_modalite_inconnue": modalite_inconnue,
        "partiels": partiels,
        "absents": absents,
        "brouillons": brouillons,
        "non_repondants_connectes": nr_connectes,
        "non_repondants_non_connectes": max(0, nr_total - nr_connectes),
        "croisement_modalite": [
            {"modalite": r["modalite"], "statut": r["statut"], "n": int(r["n"])} for r in croisement
        ],
        "definitions": {
            "presents_presentiel": "Présents contrôlés par scan du QR membre (preuve forte, nominative).",
            "presents_presentiel_declare": "Présents ayant déclaré un suivi en présentiel, sans scan (déclaratif).",
            "presents_enligne": "Présents ayant déclaré un suivi en ligne (déclaratif, aucune preuve forte en ligne n'existe).",
            "presents_modalite_inconnue": "Déclarations validées avant l'introduction de la modalité (historique).",
            "non_repondants_connectes": "Membres actifs sans participation comptée, connectés à l'application pendant la fenêtre de réponse (signal faible, jamais compté comme participation).",
            "non_repondants_non_connectes": "Membres actifs sans participation comptée et sans connexion pendant la fenêtre.",
            "population": "Membres actifs. Le comptage anonyme des activités publiques (volet B) est un autre système et n'entre jamais ici.",
        },
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
    # Global split, with the same population definitions as the per-event
    # statistics: a scan is proven on-site presence; online is a declared
    # modality, never inferred from the submission channel.
    split = db.fetch_one(
        f"""
        SELECT count(*) FILTER (WHERE {_COMPTE} AND statut = 'present') AS presents,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'partiel') AS partiels,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'absent') AS absents,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source = 'scan') AS presentiel,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source <> 'scan' AND modalite = 'presentiel') AS presentiel_declare,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND modalite = 'en_ligne') AS en_ligne,
               count(*) FILTER (WHERE {_COMPTE} AND statut = 'present' AND source <> 'scan' AND modalite IS NULL) AS modalite_inconnue
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
            "presentiel_declare": int(split.get("presentiel_declare") or 0),
            "en_ligne": int(split.get("en_ligne") or 0),
            "modalite_inconnue": int(split.get("modalite_inconnue") or 0),
        },
        "serie_evenements": [
            {"id": str(r["id"]), "titre": r["titre"], "debut": r["debut"].isoformat() if r["debut"] else None, "volet": r["volet"],
             "presents": int(r["presents"]), "partiels": int(r["partiels"]), "absents": int(r["absents"])}
            for r in serie
        ],
        "top_assidus": [{"membre": r["membre"], "matricule": r["matricule"], "presents": int(r["presents"])} for r in assidus],
        "a_relancer": [{"membre": r["membre"], "matricule": r["matricule"], "presents": int(r["presents"])} for r in a_relancer],
    }
