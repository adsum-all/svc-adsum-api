"""Single source of truth for attendance statistics.

Two tables record attendance and NEITHER is complete on its own:

* ``presence`` receives QR/manual check-ins (mode ``presentiel``, method ``qr`` or
  ``manuelle``) and online-session participations declared from the member app
  (mode ``en_ligne``, method ``lien``). It never receives a present/absent
  self-declaration.
* ``participation`` receives QR check-ins (``source = 'scan'``) and self-declared
  present/partial/absent (``source = 'declaration'``, counted only once validated).
  It never receives the online-session participations written straight to
  ``presence``.

Counting either table alone diverges; summing both double counts (a scan writes to
both). This module consolidates the two into ONE deduplicated attendance per member
(a member is counted once, even hybrid: present on site AND connected online), and
bounds every numerator to the event's TARGETED active population (the same
``CIBLE_PREDICATE`` used by the agenda, the reminders and the survey audience), so a
rate can never exceed 100 % and every derived figure reconciles by construction.

Every screen, endpoint, preview and export MUST read its per-event attendance figures
from :func:`stats_evenement` (or the SQL fragments below) so the same indicator always
yields the same number.
"""
# ruff: noqa: E501 - long, readable SQL lines
from __future__ import annotations

from . import axes_suivi as ax
from . import db
from .stats_conso import COMPTE, CONSO_CTES, POPULATION_CIBLE_CTE
from .stats_dimensions import _REPARTITION_JOINS
from .visibilite import CIBLE_PREDICATE

_STATS_EVENEMENT_SQL = f"""
WITH {CONSO_CTES}
SELECT
    (SELECT count(*) FROM cible) AS effectif_attendu,
    -- The shared vocabulary, applied to one activity. The event dashboard and the
    -- direction dashboard therefore count the same thing by construction, which they
    -- did not: this one called anyone with a "present" status a presence, online
    -- included, exactly as the aggregate one did.
    count(*) FILTER (WHERE dans_cible AND {ax.a_suivi('conso_cible')}) AS suivis,
    count(*) FILTER (WHERE dans_cible AND {ax.n_a_pas_suivi('conso_cible')}) AS absents,
    count(*) FILTER (WHERE dans_cible AND {ax.sur_place('conso_cible')}) AS presents,
    count(*) FILTER (WHERE dans_cible AND {ax.en_ligne_partiel('conso_cible')}) AS partiels,
    count(*) FILTER (WHERE dans_cible) AS repondants,
    count(*) FILTER (WHERE dans_cible AND {ax.sur_place('conso_cible')}) AS presents_presentiel,
    count(*) FILTER (WHERE dans_cible AND {ax.a_distance('conso_cible')}) AS presents_enligne,
    count(*) FILTER (WHERE dans_cible AND {ax.canal_inconnu('conso_cible')}) AS presents_modalite_inconnue,
    count(*) FILTER (WHERE dans_cible AND {ax.prouve('conso_cible')}) AS presents_scan,
    count(*) FILTER (WHERE dans_cible AND {ax.declare('conso_cible')}) AS presents_declare,
    count(*) FILTER (WHERE dans_cible AND {ax.en_ligne_entier('conso_cible')}) AS en_ligne_complet,
    count(*) FILTER (WHERE dans_cible AND {ax.en_ligne_sans_degre('conso_cible')}) AS en_ligne_sans_degre,
    count(*) FILTER (WHERE NOT dans_cible) AS hors_cible
FROM conso_cible
"""


def _taux(n: int, base: int) -> float:
    """Rate in percent, one decimal, 0.0 when the base is empty. The numerators
    passed here are always bounded to the base, so the result is always in [0, 100]."""
    return round(100.0 * n / base, 1) if base else 0.0


def stats_evenement(evenement_id: str, role: str | None) -> dict[str, object]:
    """Canonical, reconciled attendance figures for one event.

    Parameters
    ----------
    evenement_id : str
        The event identifier.
    role : str or None
        Database access scope of the caller.

    Returns
    -------
    dict
        ``effectif_attendu`` (targeted active population), ``presents``,
        ``partiels``, ``absents``, ``repondants``, ``non_repondants``, the modality
        split of the present members, ``hors_cible`` (attended but outside the
        targeted active population, for transparency) and every derived rate in
        percent. By construction ``presents + partiels + absents == repondants``,
        ``repondants + non_repondants == effectif_attendu``,
        ``presents_presentiel + presents_enligne + presents_modalite_inconnue == suivis``
        and every rate is in [0, 100].
    """
    row = db.fetch_one(_STATS_EVENEMENT_SQL, {"ev": evenement_id}, role=role) or {}
    effectif = int(row.get("effectif_attendu") or 0)
    presents = int(row.get("presents") or 0)
    partiels = int(row.get("partiels") or 0)
    absents = int(row.get("absents") or 0)
    repondants = int(row.get("repondants") or 0)
    non_repondants = max(0, effectif - repondants)
    presentiel = int(row.get("presents_presentiel") or 0)
    enligne = int(row.get("presents_enligne") or 0)
    modalite_inconnue = int(row.get("presents_modalite_inconnue") or 0)
    suivis = int(row.get("suivis") or 0)
    return {
        "effectif_attendu": effectif,
        # The three axes, named. "presents" now holds the on-site count, so a screen
        # reading it gets what the word says; "suivis" is the participation axis.
        "suivis": suivis,
        "presentiel": presentiel,
        "en_ligne": enligne,
        "presents_declare": int(row.get("presents_declare") or 0),
        "en_ligne_complet": int(row.get("en_ligne_complet") or 0),
        "en_ligne_sans_degre": int(row.get("en_ligne_sans_degre") or 0),
        "presents": presents,
        "partiels": partiels,
        "absents": absents,
        "repondants": repondants,
        "non_repondants": non_repondants,
        "presents_presentiel": presentiel,
        "presents_enligne": enligne,
        "presents_modalite_inconnue": modalite_inconnue,
        "presents_scan": int(row.get("presents_scan") or 0),
        "hors_cible": int(row.get("hors_cible") or 0),
        # Presence is the on-site count over the targeted population. Following is the
        # participation axis over the same base. Deriving one from the other by adding
        # partiel is what produced a figure that was neither.
        "taux_presence": _taux(presentiel, effectif),
        "taux_presence_physique": _taux(presentiel, effectif),
        "taux_suivi": _taux(suivis, effectif),
        "taux_participation": _taux(suivis, effectif),
        "taux_a_distance": _taux(enligne, effectif),
        "taux_reponse": _taux(repondants, effectif),
        "taux_non_reponse": _taux(non_repondants, effectif),
        "taux_absence": _taux(absents, effectif),
        "taux_partiel": _taux(partiels, effectif),
        "part_presentiel": _taux(presentiel, presents),
        "part_en_ligne": _taux(enligne, presents),
    }


# Member joins for the breakdown dimensions. The coordination is resolved from BOTH
# the member's intendance AND a direct coordination membership, so a member attached
# straight to a coordination (no intendance) is no longer classed "Sans coordination".
def repartition_evenement(evenement_id: str, dimension_expr: str, role: str | None) -> list[dict[str, object]]:
    """Break the CONSOLIDATED, targeted attendance of an event down by a dimension.

    Four counts that do not overlap and add to the targeted population, so a reader
    can check the row by adding it up: on site, online, followed by an unrecorded
    channel, did not follow.

    This used to count as ``presents`` everyone whose status said "present", which
    includes the people who followed a whole session online without leaving home. A
    breakdown of who came, that counted people who did not come, is what produced an
    on-site figure of 60,7 percent where the truth was 55,0. The predicates now come
    from :mod:`axes_suivi`, the same ones every other scope uses, so the four columns
    mean here exactly what they mean elsewhere.
    """
    rows = db.fetch_all(
        f"""
        WITH {CONSO_CTES}
        SELECT {dimension_expr} AS cle,
               count(*) FILTER (WHERE dans_cible AND {ax.sur_place('cc')}) AS presentiel,
               count(*) FILTER (WHERE dans_cible AND {ax.a_distance('cc')}) AS en_ligne,
               count(*) FILTER (WHERE dans_cible AND {ax.canal_inconnu('cc')}) AS canal_inconnu,
               count(*) FILTER (WHERE dans_cible AND {ax.a_suivi('cc')}) AS suivis,
               count(*) FILTER (WHERE dans_cible AND {ax.n_a_pas_suivi('cc')}) AS absents
        FROM conso_cible cc {_REPARTITION_JOINS}
        WHERE dans_cible
        GROUP BY {dimension_expr}
        ORDER BY suivis DESC, cle ASC
        """,
        {"ev": evenement_id},
        role=role,
    )
    return [
        {
            "cle": r["cle"],
            "presentiel": int(r["presentiel"]),
            "en_ligne": int(r["en_ligne"]),
            "canal_inconnu": int(r["canal_inconnu"]),
            "suivis": int(r["suivis"]),
            "absents": int(r["absents"]),
            # "presents" means on site, which is what the word means and what every
            # other scope returns under it. Kept so a screen reading the old name gets
            # the right figure rather than a missing one.
            "presents": int(r["presentiel"]),
            "partiels": int(r["en_ligne"]),
        }
        for r in rows
    ]



def non_repondants_detail(evenement_id: str, role: str | None, fenetre_fin_sql: str) -> dict[str, int]:
    """Split the non-respondents (targeted active members with NO consolidated record
    in either table) by whether they signed in during the response window. The total
    equals ``stats_evenement(...)['non_repondants']`` by construction (same population
    and same "no record" definition, across BOTH attendance tables)."""
    row = db.fetch_one(
        f"""
        WITH {POPULATION_CIBLE_CTE}
        SELECT count(*) AS n,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM utilisateur u JOIN session s ON s.utilisateur_id = u.id
                   WHERE u.membre_id = cible.membre_id AND s.cree_le BETWEEN e.debut AND {fenetre_fin_sql}
               )) AS connectes
        FROM cible
        JOIN evenement e ON e.id = %(ev)s
        WHERE NOT EXISTS (SELECT 1 FROM presence pr WHERE pr.evenement_id = %(ev)s AND pr.membre_id = cible.membre_id)
          AND NOT EXISTS (SELECT 1 FROM participation pa WHERE pa.evenement_id = %(ev)s AND pa.membre_id = cible.membre_id AND {COMPTE})
        """,
        {"ev": evenement_id},
        role=role,
    ) or {}
    total = int(row.get("n") or 0)
    connectes = int(row.get("connectes") or 0)
    return {"total": total, "connectes": connectes, "non_connectes": max(0, total - connectes)}


def presences_cumulees(role: str | None) -> int:
    """Total distinct attendance facts across ALL events and BOTH tables: one
    (member, event) pair counts once even if it appears in ``presence`` (scan or
    online link) and in ``participation`` (scan or a validated present/partial
    declaration). This is THE cumulative attendance figure, so the direction
    dashboard and the back-office never show two different totals for it."""
    row = db.fetch_one(
        f"""
        SELECT count(*) AS n FROM (
            SELECT DISTINCT membre_id, evenement_id FROM presence
            UNION
            SELECT membre_id, evenement_id FROM participation
            WHERE {COMPTE} AND statut IN ('present', 'partiel')
        ) faits
        """,
        (),
        role=role,
    )
    return int((row or {}).get("n") or 0)


def repartition_globale(role: str | None) -> dict[str, int]:
    """Cumulative present / partial / absent across ALL events, deduplicated per
    (member, event) over BOTH attendance tables, with the modality split of the
    present members. Same consolidation rules as :func:`stats_evenement`, so the
    global cards, the per-event screen and the direction dashboard reconcile. Not
    bounded to a target: it is a raw count of attendance facts, not a rate."""
    row = db.fetch_one(
        f"""
        WITH brut AS (
            SELECT pr.membre_id, pr.evenement_id,
                   CASE WHEN pr.mode = 'en_ligne' THEN 'en_ligne' ELSE 'presentiel' END AS modalite,
                   'present'::text AS statut, (pr.methode IN ('qr', 'manuelle')) AS scanne,
                   NULL::text AS niveau_en_ligne, false AS ambigu
            FROM presence pr
            UNION ALL
            SELECT pa.membre_id, pa.evenement_id,
                   CASE WHEN pa.source = 'scan' THEN 'presentiel'
                        WHEN pa.modalite IN ('presentiel', 'en_ligne') THEN pa.modalite ELSE NULL END,
                   pa.statut, (pa.source = 'scan'),
                   pa.niveau_en_ligne, coalesce(pa.legacy_ambigu, false)
            FROM participation pa
            WHERE {COMPTE} AND pa.statut IN ('present', 'partiel', 'absent')
        ),
        conso AS (
            SELECT membre_id, evenement_id,
                   bool_or(statut = 'present') AS present,
                   bool_or(statut = 'partiel') AS partiel,
                   bool_or(statut = 'absent') AS absent,
                   coalesce(bool_or(scanne), false) AS scanne,
                   coalesce(bool_or(modalite = 'presentiel'), false) AS a_presentiel,
                   coalesce(bool_or(modalite = 'en_ligne'), false) AS a_enligne,
                   coalesce(bool_or(niveau_en_ligne = 'complet'), false) AS en_ligne_complet,
                   coalesce(bool_or(niveau_en_ligne = 'partiel'), false) AS en_ligne_partiel,
                   coalesce(bool_or(ambigu), false) AS ambigu
            FROM brut GROUP BY membre_id, evenement_id
        )
        SELECT
            count(*) FILTER (WHERE {ax.a_suivi('conso')}) AS suivis,
            count(*) FILTER (WHERE {ax.n_a_pas_suivi('conso')}) AS absents,
            count(*) FILTER (WHERE {ax.sur_place('conso')}) AS presents,
            count(*) FILTER (WHERE {ax.prouve('conso')}) AS presentiel,
            count(*) FILTER (WHERE {ax.declare('conso')}) AS presentiel_declare,
            count(*) FILTER (WHERE {ax.a_distance('conso')}) AS en_ligne,
            count(*) FILTER (WHERE {ax.en_ligne_partiel('conso')}) AS partiels,
            count(*) FILTER (WHERE {ax.canal_inconnu('conso')}) AS modalite_inconnue
        FROM conso
        """,
        (),
        role=role,
    ) or {}
    return {
        k: int(row.get(k) or 0)
        for k in (
            "suivis", "presents", "partiels", "absents",
            "presentiel", "presentiel_declare", "en_ligne", "modalite_inconnue",
        )
    }


def serie_evenements(role: str | None, limite: int = 30) -> list[dict[str, object]]:
    """Recent events with their CONSOLIDATED present/partial/absent counts (bounded
    to each event's targeted active population), so a bar in the trend matches the
    per-event screen exactly. Most recent first."""
    rows = db.fetch_all(
        f"""
        WITH evs AS (
            SELECT id, titre, debut, volet FROM evenement ORDER BY debut DESC NULLS LAST LIMIT %(lim)s
        ),
        brut AS (
            SELECT pr.evenement_id, pr.membre_id, 'present'::text AS statut,
                   CASE WHEN pr.mode = 'en_ligne' THEN 'en_ligne' ELSE 'presentiel' END AS modalite,
                   (pr.methode IN ('qr', 'manuelle')) AS scanne,
                   NULL::text AS niveau_en_ligne, false AS ambigu
            FROM presence pr JOIN evs ON evs.id = pr.evenement_id
            UNION ALL
            SELECT pa.evenement_id, pa.membre_id, pa.statut,
                   CASE WHEN pa.source = 'scan' THEN 'presentiel'
                        WHEN pa.modalite IN ('presentiel', 'en_ligne') THEN pa.modalite ELSE NULL END,
                   (pa.source = 'scan'),
                   pa.niveau_en_ligne, coalesce(pa.legacy_ambigu, false)
            FROM participation pa JOIN evs ON evs.id = pa.evenement_id
            WHERE {COMPTE} AND pa.statut IN ('present', 'partiel', 'absent')
        ),
        conso AS (
            SELECT evenement_id, membre_id,
                   bool_or(statut = 'present') AS present,
                   bool_or(statut = 'partiel') AS partiel,
                   bool_or(statut = 'absent') AS absent,
                   coalesce(bool_or(scanne), false) AS scanne,
                   coalesce(bool_or(modalite = 'presentiel'), false) AS a_presentiel,
                   coalesce(bool_or(modalite = 'en_ligne'), false) AS a_enligne,
                   coalesce(bool_or(niveau_en_ligne = 'complet'), false) AS en_ligne_complet,
                   coalesce(bool_or(niveau_en_ligne = 'partiel'), false) AS en_ligne_partiel,
                   coalesce(bool_or(ambigu), false) AS ambigu
            FROM brut GROUP BY evenement_id, membre_id
        ),
        cible AS (
            SELECT evs.id AS evenement_id, m.id AS membre_id
            FROM evs JOIN evenement e ON e.id = evs.id
            JOIN membre m ON m.statut = 'actif'
            LEFT JOIN intendance mi ON mi.id = m.intendance_id
            WHERE {CIBLE_PREDICATE}
        ),
        agg AS (
            SELECT c.evenement_id,
                   count(*) FILTER (WHERE {ax.a_suivi('c')}) AS suivis,
                   count(*) FILTER (WHERE {ax.sur_place('c')}) AS presents,
                   count(*) FILTER (WHERE {ax.sur_place('c')}) AS presentiel,
                   count(*) FILTER (WHERE {ax.a_distance('c')}) AS en_ligne,
                   count(*) FILTER (WHERE {ax.en_ligne_partiel('c')}) AS partiels,
                   count(*) FILTER (WHERE {ax.n_a_pas_suivi('c')}) AS absents
            FROM conso c JOIN cible ci ON ci.evenement_id = c.evenement_id AND ci.membre_id = c.membre_id
            GROUP BY c.evenement_id
        )
        SELECT evs.id, evs.titre, evs.debut, evs.volet,
               coalesce(agg.suivis, 0) AS suivis,
               coalesce(agg.presents, 0) AS presents,
               coalesce(agg.presentiel, 0) AS presentiel,
               coalesce(agg.en_ligne, 0) AS en_ligne,
               coalesce(agg.partiels, 0) AS partiels,
               coalesce(agg.absents, 0) AS absents
        FROM evs LEFT JOIN agg ON agg.evenement_id = evs.id
        ORDER BY evs.debut DESC NULLS LAST
        """,
        {"lim": limite},
        role=role,
    )
    return [
        {
            "id": str(r["id"]), "titre": r["titre"],
            "debut": r["debut"].isoformat() if r["debut"] else None,
            "volet": r["volet"],
            # The three axes, so a screen never has to add two of them to guess a third.
            "suivis": int(r["suivis"] or 0),
            "presentiel": int(r["presentiel"] or 0),
            "en_ligne": int(r["en_ligne"] or 0),
            "presents": int(r["presents"] or 0),
            "partiels": int(r["partiels"] or 0),
            "absents": int(r["absents"] or 0),
        }
        for r in rows
    ]


def assiduite_perimetre(
    where_sql: str, params: tuple[object, ...],
    fen_where: str, fen_params: tuple[object, ...],
    jours: int, role: str | None,
) -> tuple[int, list[dict[str, object]]]:
    """Consolidated attendance over a rolling window for the members matched by
    ``where_sql`` (alias ``m``), counted over the events matched by ``fen_where``
    (alias ``e``, the perimeter's relevant activities), so both the events count and
    each member's presences are bounded to the same perimeter (no global leak).
    ``presences`` counts DISTINCT events attended across BOTH tables (presence and
    counted participation), so a member who only declared an online participation is
    no longer wrongly seen as absent. Cancelled activities are excluded. Returns
    (number of events in the window, per-member rows)."""
    fen_clause = f"({fen_where}) AND e.debut >= now() - make_interval(days => %s) AND e.debut <= now() AND coalesce(e.annule, false) = false"
    fen = db.fetch_one(
        f"SELECT count(*) AS n FROM evenement e WHERE {fen_clause}",
        (*fen_params, jours),
        role=role,
    )
    rows = db.fetch_all(
        f"""
        WITH fen AS (
            SELECT e.id FROM evenement e WHERE {fen_clause}
        ),
        attend AS (
            SELECT DISTINCT membre_id, evenement_id FROM (
                SELECT membre_id, evenement_id FROM presence WHERE evenement_id IN (SELECT id FROM fen)
                UNION
                SELECT membre_id, evenement_id FROM participation
                WHERE evenement_id IN (SELECT id FROM fen) AND {COMPTE} AND statut IN ('present', 'partiel')
            ) x
        )
        SELECT m.id, m.matricule, m.prenoms, m.nom, count(a.evenement_id) AS presences
        FROM membre m LEFT JOIN attend a ON a.membre_id = m.id
        WHERE {where_sql}
        GROUP BY m.id, m.matricule, m.prenoms, m.nom
        ORDER BY presences ASC, m.nom ASC
        LIMIT 500
        """,
        (*fen_params, jours, *params),
        role=role,
    )
    return int((fen or {}).get("n") or 0), rows


def presents_membre_ids(evenement_id: str, role: str | None) -> list[str]:
    """Deduplicated ids of the members counted PRESENT for an event (targeted active
    population), from the consolidated attendance. Used to cross-check that two
    screens or an export never disagree on who attended."""
    rows = db.fetch_all(
        f"WITH {CONSO_CTES} SELECT membre_id FROM conso_cible WHERE dans_cible AND present",
        {"ev": evenement_id},
        role=role,
    )
    return [str(r["membre_id"]) for r in rows]
