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

from . import db, visibilite
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["participation"])

# The per-member analytic profile lives inside the member file, which is reserved
# to the member-managing roles (same set as admin.get_membre): a field scanner
# (controleur) only scans attendance and read-only supervision (direction) works
# from aggregates, so neither reads one member's individual behaviour.

_STATUTS = ("present", "partiel", "absent")
_MODALITES = ("presentiel", "en_ligne")

# A participation is counted when it is a scan or a validated declaration.
_COMPTE = "(source = 'scan' OR valide)"

# Diffusion threshold: below this many anonymous evaluations, the mean and the
# distribution are withheld so a rating cannot be inferred for a small cohort.
_SEUIL_EVAL = 3

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
    if not visibilite.evenement_visible_membre(evenement_id, membre_id, role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
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
    # The member's own evaluation is ANONYMOUS: it is never read back to them
    # (there is no member link to it). We only tell them whether they have already
    # used their one-time evaluation, never its content.
    deja_evalue = bool(db.fetch_one(
        "SELECT 1 FROM evaluation_activite_droit WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id), role=role,
    ))
    row = db.fetch_one(
        "SELECT statut, source, valide, modalite FROM participation WHERE evenement_id = %s AND membre_id = %s",
        (evenement_id, membre_id),
        role=role,
    )
    if not row:
        return {"statut": None, "source": None, "valide": False, "deja_evalue": deja_evalue, "modalite": None, "deja_scanne": False, "verrouille": False, "ouvert": ouvert, "disponible_le": disponible_le, "cloture": cloture, "cloture_le": cloture_le}
    scanne = row["source"] == "scan"
    # Finalized (validated) participation is fully immutable. A scanned member is
    # present but may still give their feedback once (which finalizes it).
    verrouille = bool(row["valide"])
    return {
        "statut": row["statut"],
        "source": row["source"],
        "valide": bool(row["valide"]),
        "deja_evalue": deja_evalue,
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
    if not visibilite.evenement_visible_membre(evenement_id, membre_id, role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
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

    upsert_participation(
        evenement_id,
        membre_id,
        statut=new_statut,
        source=new_source,
        valide=payload.valider,
        modalite=new_modalite,
        role=role,
    )
    # The rating/comment are stored ANONYMOUSLY, in a separate table with no member
    # link, so presence stays identifiable while the evaluation cannot be attributed.
    evalue = soumettre_evaluation(evenement_id, membre_id, note=payload.note, avis=payload.avis, role=role)
    return {"ok": True, "verrouille": payload.valider, "statut": new_statut, "valide": payload.valider, "modalite": new_modalite, "evaluation_enregistree": evalue}


def upsert_participation(
    evenement_id: str,
    membre_id: str,
    *,
    statut: str,
    source: str,
    valide: bool,
    modalite: str | None,
    role: str | None = None,
) -> None:
    """Write a participation row (PRESENCE only), converging every channel onto one
    source of truth. The rating and comment are NOT stored here: they go to the
    anonymous ``evaluation_activite`` table via :func:`soumettre_evaluation`, so a
    member is never linked to their evaluation.

    This is THE single write path for the ``participation`` table shared by the
    member space, the QR scan reconciliation and the external check-in link, so a
    person is counted exactly once whatever the channel. The upsert is atomic on
    the ``(evenement_id, membre_id)`` unique key: a scan always wins (its presence
    and modality are never downgraded by a concurrent declaration), and a
    finalized (``valide``) row is never touched (``WHERE NOT participation.valide``).
    """
    db.execute(
        """
        INSERT INTO participation (evenement_id, membre_id, statut, source, valide, modalite)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (evenement_id, membre_id)
        DO UPDATE SET
            statut = CASE WHEN participation.source = 'scan' THEN 'present' ELSE EXCLUDED.statut END,
            source = CASE WHEN participation.source = 'scan' THEN 'scan' ELSE EXCLUDED.source END,
            modalite = CASE WHEN participation.source = 'scan' THEN 'presentiel' ELSE EXCLUDED.modalite END,
            valide = EXCLUDED.valide,
            maj_le = now()
        WHERE NOT participation.valide
        """,
        (evenement_id, membre_id, statut, source, valide, modalite),
        role=role,
    )


def soumettre_evaluation(
    evenement_id: str,
    membre_id: str,
    *,
    note: int | None,
    avis: str | None,
    role: str | None = None,
) -> bool:
    """Store an ANONYMOUS activity evaluation (rating 1..5 and/or free comment).

    Anonymity by design. In a single atomic statement: claim the member's one-time
    evaluation right (``evaluation_activite_droit``, member-keyed, NO content) and,
    only if the claim succeeds, insert the content (``evaluation_activite``, NO
    member link, coarse date only). The ``membre_id`` therefore never sits next to a
    note or a comment, and the two rows share no persisted key: the evaluation can
    never be attributed to the member, while a second evaluation is a no-op.

    Returns True when a new anonymous evaluation was recorded, False when there was
    nothing to record or the member had already evaluated this activity."""
    avis_clean = avis.strip() if isinstance(avis, str) else None
    if note is None and not avis_clean:
        return False
    row = db.execute(
        """
        WITH claim AS (
            INSERT INTO evaluation_activite_droit (evenement_id, membre_id) VALUES (%s, %s)
            ON CONFLICT (evenement_id, membre_id) DO NOTHING
            RETURNING evenement_id
        )
        INSERT INTO evaluation_activite (evenement_id, note, avis)
        SELECT evenement_id, %s, %s FROM claim
        RETURNING id
        """,
        (evenement_id, membre_id, note, avis_clean or None),
        role=role,
    )
    return bool(row)


# --- Admin: per-event statistics --------------------------------------------

@router.get("/admin/evenements/{evenement_id}/participation-stats")
def participation_stats(evenement_id: str, user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> dict[str, object]:
    from . import stats_core

    ev = db.fetch_one("SELECT id, titre, debut, volet FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    # Every headline figure comes from the single canonical layer: the denominator is
    # the event's targeted active population, attendance is deduplicated across the
    # presence and participation tables, and numerators are bounded to that population.
    s = stats_core.stats_evenement(evenement_id, user.role)
    presents = s["presents"]
    presentiel = s["presents_presentiel"]  # all on-site (scan or declared)
    scan = s["presents_scan"]
    # Draft (unvalidated) participations, for the follow-up counters.
    brouillons_row = db.fetch_one(
        f"SELECT count(*) FILTER (WHERE NOT {_COMPTE}) AS brouillons FROM participation WHERE evenement_id = %s",
        (evenement_id,),
        role=user.role,
    ) or {}
    # Ratings come from the ANONYMOUS evaluation table (no member link). Below the
    # diffusion threshold, the mean and the distribution are withheld so a rating
    # can never be inferred for a small cohort; only the count of evaluations shows.
    ev_row = db.fetch_one(
        "SELECT round(avg(note), 2) AS note_moyenne, count(note) AS nb_notes, count(*) AS nb_eval "
        "FROM evaluation_activite WHERE evenement_id = %s",
        (evenement_id,),
        role=user.role,
    ) or {}
    nb_eval = int(ev_row.get("nb_eval") or 0)
    seuil_eval_ok = nb_eval >= _SEUIL_EVAL
    notes_dist = db.fetch_all(
        "SELECT note, count(*) AS n FROM evaluation_activite WHERE evenement_id = %s AND note IS NOT NULL GROUP BY note ORDER BY note",
        (evenement_id,),
        role=user.role,
    ) if seuil_eval_ok else []
    notes = {
        "note_moyenne": ev_row.get("note_moyenne") if seuil_eval_ok else None,
        "nb_notes": int(ev_row.get("nb_notes") or 0),
        "brouillons": brouillons_row.get("brouillons") or 0,
    }
    # Modality x follow-up cross table (counted participations only).
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
    # Non-respondents (targeted active members with NO record in either table),
    # split by connectivity. Its total equals s['non_repondants'] by construction.
    nr = stats_core.non_repondants_detail(evenement_id, user.role, FENETRE_FIN_SQL)
    nb_notes = int(notes.get("nb_notes") or 0)

    def taux(n: int, base: int) -> float:
        return round(100.0 * n / base, 1) if base else 0.0

    return {
        "evenement": {"id": str(ev["id"]), "titre": ev["titre"], "debut": ev["debut"].isoformat() if ev["debut"] else None, "volet": ev["volet"]},
        "effectif_attendu": s["effectif_attendu"],
        "repondants": s["repondants"],
        "non_repondants": s["non_repondants"],
        "presents": presents,
        "presents_presentiel": scan,  # scan-proven on-site (strong proof)
        "presents_presentiel_declare": max(0, presentiel - scan),  # declared on-site, no scan
        "presents_enligne": s["presents_enligne"],
        "presents_modalite_inconnue": s["presents_modalite_inconnue"],
        "partiels": s["partiels"],
        "absents": s["absents"],
        "brouillons": int(notes.get("brouillons") or 0),
        "hors_cible": s["hors_cible"],
        "non_repondants_connectes": nr["connectes"],
        "non_repondants_non_connectes": nr["non_connectes"],
        "croisement_modalite": [
            {"modalite": r["modalite"], "statut": r["statut"], "n": int(r["n"])} for r in croisement
        ],
        "definitions": {
            "effectif_attendu": "Membres actifs CIBLES par l'activite (tous les actifs pour une activite generale, sinon les membres de l'unite visee). Denominateur unique de tous les taux.",
            "presents_presentiel": "Presents controles par scan du QR membre (preuve forte, nominative).",
            "presents_presentiel_declare": "Presents ayant declare un suivi en presentiel, sans scan (declaratif).",
            "presents_enligne": "Presents ayant declare ou enregistre un suivi en ligne (declaratif).",
            "presents_modalite_inconnue": "Declarations validees avant l'introduction de la modalite (historique).",
            "hors_cible": "Personnes vues presentes mais hors de la population active ciblee (comptees a part, jamais dans les taux).",
            "non_repondants_connectes": "Membres cibles sans presence comptee, connectes pendant la fenetre de reponse (signal faible).",
            "non_repondants_non_connectes": "Membres cibles sans presence comptee et sans connexion pendant la fenetre.",
            "population": "Le comptage anonyme des activites publiques (volet B) est un autre systeme et n'entre jamais ici.",
        },
        "taux_reponse": s["taux_reponse"],
        "taux_non_reponse": s["taux_non_reponse"],
        "taux_presence": s["taux_presence"],
        "taux_presence_repondants": taux(presents, s["repondants"]),
        "taux_participation": s["taux_participation"],
        "taux_partiel": s["taux_partiel"],
        "taux_absence": s["taux_absence"],
        "part_presentiel": s["part_presentiel"],
        "part_en_ligne": s["part_en_ligne"],
        "note_moyenne": float(notes["note_moyenne"]) if notes.get("note_moyenne") is not None else None,
        "nb_notes": nb_notes,
        "nb_evaluations": nb_eval,
        "evaluation_anonyme": True,
        "seuil_evaluation": _SEUIL_EVAL,
        "seuil_evaluation_atteint": seuil_eval_ok,
        "taux_reponse_note": taux(nb_notes, presents),
        "distribution_notes": [{"note": int(r["note"]), "n": int(r["n"])} for r in notes_dist],
        "repartitions": {dim: stats_core.repartition_evenement(evenement_id, expr, user.role) for dim, expr in stats_core.REPARTITION_DIMENSIONS.items()},
    }


# --- Admin: global participation trends -------------------------------------

@router.get("/admin/participation/global")
def participation_global(user: Annotated[UserMe, Depends(require_permission("participation.consulter"))]) -> dict[str, object]:
    from . import stats_core

    role = user.role
    # Time series and global split both come from the single canonical layer, so a
    # bar in the trend matches the per-event screen and the global cards reconcile
    # with the direction dashboard (deduplicated across the two attendance tables).
    serie = stats_core.serie_evenements(role, limite=30)
    split = stats_core.repartition_globale(role)
    nb_evenements = db.fetch_one("SELECT count(*) AS n FROM evenement", (), role=role)
    total_ev = int(nb_evenements["n"]) if nb_evenements else 0
    # Anonymous attendance cohorts over the rolling window (admin parameter
    # fenetre_assiduite_jours, default 90 days): how many active members sit in
    # each presence-rate band. No nominative ranking: individual figures are
    # only shown inside a member's own file, never as a public podium or a
    # name-and-shame list.
    fenetre_jours = db.fetch_one(
        "SELECT coalesce((SELECT (valeur #>> '{}')::int FROM parametre WHERE cle = 'fenetre_assiduite_jours'), 90) AS j",
        (),
        role=role,
    )
    jours = int((fenetre_jours or {}).get("j") or 90)
    distribution = db.fetch_all(
        f"""
        WITH fen AS (
            SELECT id FROM evenement
            WHERE debut >= now() - make_interval(days => %s) AND debut <= now()
        ), taux AS (
            SELECT m.id,
                   CASE WHEN (SELECT count(*) FROM fen) = 0 THEN NULL
                        ELSE 100.0 * count(p.*) FILTER (WHERE {_COMPTE} AND p.statut IN ('present', 'partiel'))
                             / (SELECT count(*) FROM fen)
                   END AS t
            FROM membre m
            LEFT JOIN participation p ON p.membre_id = m.id AND p.evenement_id IN (SELECT id FROM fen)
            WHERE m.statut = 'actif'
            GROUP BY m.id
        )
        SELECT CASE WHEN t IS NULL THEN 'sans_donnee'
                    WHEN t >= 75 THEN '75_100'
                    WHEN t >= 50 THEN '50_74'
                    WHEN t >= 25 THEN '25_49'
                    ELSE '0_24' END AS tranche,
               count(*) AS membres
        FROM taux GROUP BY 1
        """,
        (jours,),
        role=role,
    )
    # Monthly counted participation over the last 6 months (trend line).
    evolution = db.fetch_all(
        f"""
        SELECT to_char(date_trunc('month', mois), 'YYYY-MM') AS mois,
               count(p.*) FILTER (WHERE {_COMPTE} AND p.statut IN ('present', 'partiel')) AS participations
        FROM generate_series(date_trunc('month', current_date) - interval '5 months',
                             date_trunc('month', current_date), interval '1 month') AS mois
        LEFT JOIN evenement e ON date_trunc('month', e.debut) = date_trunc('month', mois)
        LEFT JOIN participation p ON p.evenement_id = e.id
        GROUP BY mois ORDER BY mois ASC
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
        "serie_evenements": serie,
        # Ethics: no nominative podium nor name-and-shame list. Attendance is
        # exposed as anonymous cohorts here; individual figures live only in
        # the member's own file (accompaniment, not exposure).
        "fenetre_assiduite_jours": jours,
        "distribution_assiduite": [
            {"tranche": r["tranche"], "membres": int(r["membres"])} for r in distribution
        ],
        "evolution_mensuelle": [
            {"mois": r["mois"], "participations": int(r["participations"])} for r in evolution
        ],
        "definitions": {
            "distribution_assiduite": (
                "Cohortes anonymes de membres actifs par taux de participation (présent ou partiel, comptés) "
                f"aux événements des {jours} derniers jours. 'sans_donnee' = aucun événement sur la fenêtre. "
                "Interprétation prudente si peu d'événements."
            ),
            "evolution_mensuelle": "Participations comptées (présent ou partiel, scan ou déclaration validée) par mois, 6 derniers mois.",
        },
    }


# --- Admin: per-member analytics (inside the member file only) ---------------

@router.get("/admin/membres/{membre_id}/participation-analytique")
def participation_membre(membre_id: str, user: Annotated[UserMe, Depends(require_permission("participation.gerer"))]) -> dict[str, object]:
    """Individual participation view for one member's file.

    Meant for understanding and accompaniment, never for ranking: it is only
    reachable from the member's own file. Figures are computed on the rolling
    attendance window (admin parameter, default 90 days) with the same counting
    rules as every other statistic (scan or validated declaration).
    """
    role = user.role
    fen = db.fetch_one(
        "SELECT coalesce((SELECT (valeur #>> '{}')::int FROM parametre WHERE cle = 'fenetre_assiduite_jours'), 90) AS j",
        (),
        role=role,
    )
    jours = int((fen or {}).get("j") or 90)
    agg = db.fetch_one(
        f"""
        WITH fen AS (
            SELECT id, debut FROM evenement
            WHERE debut >= now() - make_interval(days => %s) AND debut <= now()
        )
        SELECT (SELECT count(*) FROM fen) AS evenements,
               (SELECT count(*) FROM fen WHERE debut >= now() - make_interval(days => %s)) AS evenements_recents,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present') AS presents,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present' AND p.source = 'scan') AS presents_prouves,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'present' AND p.source <> 'scan' AND p.modalite = 'en_ligne') AS presents_en_ligne,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'partiel') AS partiels,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut = 'absent') AS absents,
               count(*) FILTER (WHERE {_COMPTE} AND p.statut IN ('present', 'partiel')
                                AND (SELECT debut FROM fen f WHERE f.id = p.evenement_id) >= now() - make_interval(days => %s)) AS participations_recentes
        FROM participation p
        WHERE p.membre_id = %s AND p.evenement_id IN (SELECT id FROM fen)
        """,
        (jours, jours // 2, jours // 2, membre_id),
        role=role,
    ) or {}
    historique = db.fetch_all(
        """
        SELECT e.titre, e.debut, p.statut, p.modalite, p.source,
               (p.source = 'scan' OR p.valide) AS compte
        FROM evenement e
        LEFT JOIN participation p ON p.evenement_id = e.id AND p.membre_id = %s
        WHERE e.debut <= now()
        ORDER BY e.debut DESC LIMIT 8
        """,
        (membre_id,),
        role=role,
    )
    evenements = int(agg.get("evenements") or 0)
    ev_recents = int(agg.get("evenements_recents") or 0)
    presents = int(agg.get("presents") or 0)
    partiels = int(agg.get("partiels") or 0)
    participations = presents + partiels
    part_recentes = int(agg.get("participations_recentes") or 0)
    taux = round(100.0 * participations / evenements, 1) if evenements else None
    taux_recent = round(100.0 * part_recentes / ev_recents, 1) if ev_recents else None
    taux_anterieur = (
        round(100.0 * (participations - part_recentes) / (evenements - ev_recents), 1)
        if evenements - ev_recents > 0
        else None
    )
    return {
        "fenetre_jours": jours,
        "evenements_fenetre": evenements,
        "presents": presents,
        "presents_prouves": int(agg.get("presents_prouves") or 0),
        "presents_en_ligne": int(agg.get("presents_en_ligne") or 0),
        "partiels": partiels,
        "absents": int(agg.get("absents") or 0),
        "sans_reponse": max(0, evenements - participations - int(agg.get("absents") or 0)),
        "taux_participation": taux,
        "taux_recent": taux_recent,
        "taux_anterieur": taux_anterieur,
        "historique": [
            {
                "titre": r["titre"],
                "debut": r["debut"].isoformat() if r["debut"] else None,
                "statut": (r["statut"] if r.get("compte") else None),
                "modalite": r.get("modalite"),
                "prouve": r.get("source") == "scan",
            }
            for r in historique
        ],
        "avertissement": (
            f"Lecture sur {evenements} événement(s) en {jours} jours : interprétation prudente si l'effectif "
            "d'événements est faible. Vue destinée à l'accompagnement du membre, pas au classement."
        ),
    }
