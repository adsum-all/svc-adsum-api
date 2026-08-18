"""Analytical layer of the direction dashboard: filters, cross-tabs, drill-down.

The direction reads one thing, attendance, and needs to cut it many ways: over a
period, inside a coordination, by commission crossed with modality, comparing this
quarter with the last. The previous breakdown answered one question, "how does this
one dimension split", with no filter at all, so the screens could show a total and
never the story behind it.

Everything here rests on ONE consolidation, defined once in :data:`_CONSO`, and every
figure any screen shows is cut from it. That is the whole reliability argument: a bar
in a cross-tab, a rate in a drill-down and a KPI card cannot disagree, because they
are the same rows grouped differently. Where a number could be read two ways, the
narrower reading wins and the response says which one it used.

Three rules the direction's trust depends on.

One member, one activity, one row. Attendance lives in two tables, a scan writes to
both, and summing them double counts. The consolidation deduplicates on (member,
activity) and keeps the strongest status, so the controller application scanning
somebody who also answered the survey produces one attendance, not two, and that
person is counted present on site rather than twice.

A dimension is never a caller's string. Every dimension and every filter maps to a
fixed SQL expression declared here; anything else is refused. A dashboard whose axis
can be dictated from outside is a dashboard whose numbers cannot be vouched for.

Nothing is inferred from thin data. A rate over three activities is not a trend, and
the payload carries the volume it was computed from so the screen can say so instead
of drawing a confident line through noise.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Any

from . import axes_suivi as ax
from . import db
from .direction_dimensions import (
    _FILTRES,
    _FILTRES_ENUMERES,
    _INCONNU,
    DIMENSIONS,
)
from .stats_core import COMPTE
from .visibilite import CIBLE_PREDICATE

# ---------------------------------------------------------------------------
# Consolidation: the single source every figure below is cut from.
# ---------------------------------------------------------------------------

#: One row per (member, activity), across BOTH attendance tables.
#:
#: ``presence`` holds QR/manual check-ins and online-session participations;
#: ``participation`` holds scans and validated self-declarations. A scan writes to
#: both, so summing them counts that member twice. Deduplicating here is what makes
#: the controller application's scan and the member's survey answer one fact.
#:
#: The modality is resolved with proof first: a scan is on-site whatever was declared
#: afterwards, because it is the only channel that proves physical presence.
_CONSO = f"""
brut AS (
    SELECT pr.membre_id, pr.evenement_id,
           CASE WHEN pr.mode = 'en_ligne' THEN 'en_ligne' ELSE 'presentiel' END AS modalite,
           'present'::text AS statut,
           (pr.methode IN ('qr', 'manuelle')) AS scanne,
           NULL::text AS niveau_en_ligne,
           NULL::text AS absence_motif,
           NULL::text AS absence_qualification,
           false AS ambigu
    FROM presence pr
    UNION ALL
    SELECT pa.membre_id, pa.evenement_id,
           CASE WHEN pa.source = 'scan' THEN 'presentiel'
                WHEN pa.mode_suivi IN ('presentiel', 'en_ligne') THEN pa.mode_suivi
                WHEN pa.modalite IN ('presentiel', 'en_ligne') THEN pa.modalite
                ELSE NULL END AS modalite,
           pa.statut,
           (pa.source = 'scan') AS scanne,
           pa.niveau_en_ligne,
           pa.absence_motif,
           pa.absence_qualification,
           coalesce(pa.legacy_ambigu, false) AS ambigu
    FROM participation pa
    WHERE {COMPTE} AND pa.statut IN ('present', 'partiel', 'absent')
),
-- Everybody the activity concerned, whether or not they left a trace.
--
-- Without this the denominator was "people who answered". Roughly a third of the
-- expected audience never answers, and they are the least likely to attend, so
-- excluding them raised every rate without anything changing on the ground. They now
-- appear in a state of their own, `sans_information`, which is neither an attendance
-- nor an absence: nobody knows.
--
-- Bounded by the activity's own targeting and by the date the member joined, so a
-- person who arrived in June is not counted missing from March.
cible AS (
    SELECT e.id AS evenement_id, m.id AS membre_id
    FROM evenement e
    JOIN membre m ON m.statut = 'actif'
    LEFT JOIN intendance mi ON mi.id = m.intendance_id
    WHERE coalesce(e.annule, false) = false
      AND e.debut <= now()
      AND e.debut >= coalesce(m.date_entree::timestamptz, m.cree_le, '-infinity')
      AND {CIBLE_PREDICATE}
),
-- Union rather than the target alone: a record belonging to somebody outside the
-- target, an inactive member scanned at the door for instance, is still a fact and
-- must not be dropped by the very query meant to widen the denominator.
paires AS (
    SELECT evenement_id, membre_id FROM brut
    UNION
    SELECT evenement_id, membre_id FROM cible
),
conso AS (
    SELECT p.membre_id, p.evenement_id,
           coalesce(bool_or(statut = 'present'), false) AS present,
           coalesce(bool_or(statut = 'partiel'), false) AS partiel,
           coalesce(bool_or(statut = 'absent'), false) AS absent,
           coalesce(bool_or(scanne), false) AS scanne,
           coalesce(bool_or(modalite = 'presentiel'), false) AS a_presentiel,
           coalesce(bool_or(modalite = 'en_ligne'), false) AS a_enligne,
           -- Online attendance has a degree, on-site attendance does not.
           coalesce(bool_or(niveau_en_ligne = 'complet'), false) AS en_ligne_complet,
           coalesce(bool_or(niveau_en_ligne = 'partiel'), false) AS en_ligne_partiel,
           -- A row the old model cannot express. Counted apart, never inside a rate:
           -- nobody knows whether "partial, on site" meant arriving late or following
           -- intermittently, and averaging a guess is how a dashboard lies quietly.
           -- Why the person did not follow, and whether a habilitated responsible has
           -- ruled on it. max() over the group because a member has at most one
           -- declaration per activity: the two attendance tables cannot disagree here,
           -- only one of them carries a reason at all.
           max(absence_motif) AS absence_motif,
           max(absence_qualification) AS absence_qualification,
           coalesce(bool_or(ambigu), false) AS ambigu
    FROM paires p
    LEFT JOIN brut b ON b.membre_id = p.membre_id AND b.evenement_id = p.evenement_id
    GROUP BY p.membre_id, p.evenement_id
)
"""

#: Rows a rate may be computed on. Ambiguous legacy rows are excluded everywhere a
#: percentage is produced, and counted separately so their exclusion is visible rather
#: than silent.
_EXPLOITABLE = "NOT cc.ambigu"

#: Joins that make every dimension below reachable from a consolidated row.
_JOINTURES = """
JOIN membre m ON m.id = cc.membre_id
JOIN evenement e ON e.id = cc.evenement_id
LEFT JOIN commission c ON c.id = m.commission_id
LEFT JOIN intendance i ON i.id = m.intendance_id
LEFT JOIN coordination co ON co.id = i.coordination_id
LEFT JOIN coordination cod ON cod.id = m.coordination_id
LEFT JOIN tribu t ON t.id = m.tribu_id
LEFT JOIN type_evenement te ON te.id = e.type_evenement_id
"""

class DimensionInconnue(ValueError):
    """A dimension or filter the platform does not compute. Refused, never guessed."""


def dimensions_disponibles() -> list[dict[str, str]]:
    """The axes a screen may offer, with their label and family."""
    return [
        {"cle": cle, "libelle": meta["libelle"], "famille": meta["famille"]}
        for cle, meta in DIMENSIONS.items()
    ]


def filtres_disponibles() -> list[str]:
    """The filter keys a screen may send."""
    return sorted([*_FILTRES, *_FILTRES_ENUMERES])


def valeurs_enumerees() -> dict[str, list[str]]:
    """The accepted values of the filters that are a choice rather than an identifier."""
    return {cle: sorted(vals) for cle, vals in _FILTRES_ENUMERES.items()}


def _expr(dimension: str) -> str:
    meta = DIMENSIONS.get(dimension)
    if meta is None:
        raise DimensionInconnue(dimension)
    return meta["expr"]


def _where(filtres: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Turn the requested filters into a WHERE fragment and its bound parameters.

    An unknown filter raises rather than being ignored: silently dropping one would
    show the direction a wider population than they asked for, under a heading saying
    otherwise, which is the one failure a filtered dashboard must not have.
    """
    morceaux: list[str] = []
    params: dict[str, Any] = {}
    for cle, valeur in filtres.items():
        if valeur in (None, ""):
            continue
        # A choice filter selects one of a fixed set of fragments. The value never
        # reaches the SQL, so an unexpected one is refused rather than interpolated.
        choix = _FILTRES_ENUMERES.get(cle)
        if choix is not None:
            fragment = choix.get(str(valeur))
            if fragment is None:
                raise DimensionInconnue(
                    f"{cle} : valeur inconnue « {valeur} ». Attendu : {', '.join(sorted(choix))}."
                )
            morceaux.append(fragment)
            continue
        fragment = _FILTRES.get(cle)
        if fragment is None:
            raise DimensionInconnue(cle)
        morceaux.append(fragment)
        params[f"f_{cle}"] = valeur
    return (" AND ".join(morceaux) if morceaux else "true"), params


def _taux(n: int, base: int) -> float:
    return round(100.0 * n / base, 1) if base else 0.0


def _ligne(r: dict[str, Any], cle: str = "cle") -> dict[str, Any]:
    """One breakdown row, with its rates already computed.

    The rates travel with the counts rather than being recomputed by each screen:
    two screens dividing the same numbers their own way is how a dashboard starts
    contradicting itself.
    """
    # The counts now come straight from the shared vocabulary, so the three axes stay
    # separate here too. "presents" is the on-site count, not the follow count: adding
    # partiel to it used to produce a "suivis" that was really "on site plus partial
    # online", which is not a population anybody names.
    suivis = int(r.get("suivis") or 0)
    absents = int(r.get("absents") or 0)
    sans_info = int(r.get("sans_information") or 0)
    # The denominator is the whole expected audience: those who followed, those who
    # said they did not, and those who never answered. It used to stop at the first
    # two, which is the "people who answered" denominator. That one flatters, because
    # the people who never answer are the least likely to have attended.
    total = suivis + absents + sans_info
    # Kept because some questions are genuinely about the people who did answer, such
    # as "of those who told us, how many gave a reason". Published under its own name
    # so no screen can mistake one for the other.
    repondants = suivis + absents
    presentiel = int(r.get("presentiel") or 0)
    en_ligne = int(r.get("en_ligne") or 0)
    prouve = int(r.get("presentiel_prouve") or 0)
    declare = int(r.get("presentiel_declare") or 0)
    presents = presentiel
    partiels = int(r.get("partiels") or 0)
    return {
        "label": r.get(cle) or _INCONNU,
        "presents": presents,
        "partiels": partiels,
        "absents": absents,
        "sans_information": sans_info,
        # `total` is the denominator of every rate here and therefore excludes the
        # uninterpretable rows. `lignes` is what the base actually holds, so a group
        # made entirely of such rows shows its size instead of reading as empty.
        "total": total,
        "repondants": repondants,
        "taux_reponse": _taux(repondants, total),
        "lignes": total + int(r.get("non_interpretables") or 0),
        "suivis": suivis,
        "presentiel": presentiel,
        "en_ligne": en_ligne,
        "en_ligne_sans_degre": int(r.get("en_ligne_sans_degre") or 0),
        "scannes": prouve,
        # The four buckets a follow can fall into, each named for what it is. They sum
        # to the number of people who followed, so a reader can check the breakdown
        # against the headline instead of trusting it.
        "presentiel_prouve": prouve,
        "presentiel_declare": declare,
        "en_ligne_complet": int(r.get("en_ligne_complet") or 0),
        "en_ligne_partiel": int(r.get("en_ligne_partiel") or 0),
        "suivi_modalite_inconnue": int(r.get("suivi_modalite_inconnue") or 0),
        # Excluded from every rate above, reported so the exclusion is visible.
        "non_interpretables": int(r.get("non_interpretables") or 0),
        # Presence is the channel axis: on site, and nothing else. Following is the
        # participation axis. Both are published so a screen never has to derive one
        # from the other and get it wrong.
        "taux_presence": _taux(presentiel, total),
        "taux_presence_physique": _taux(presentiel, total),
        "taux_suivi": _taux(suivis, total),
        "taux_participation": _taux(suivis, total),
        "taux_a_distance": _taux(en_ligne, total),
        "taux_absence": _taux(absents, total),
        # How much of the attendance is evidence rather than assertion. A unit at
        # eighty per cent on declarations alone is a different situation from one at
        # eighty per cent proven at the door, and the figure alone cannot say which.
        "taux_preuve": _taux(prouve, suivis),
    }


#: One expression per fact the direction reads, computed once so a bar, a rate and a
#: card cannot disagree. Every count excludes the ambiguous legacy rows, which are
#: reported separately: a rate over rows nobody can interpret is not a rate.
_AGREGATS = f"""
    -- Every count comes from the shared vocabulary in axes_suivi. Six modules each
    -- wrote their own version of these predicates and drifted: one counted a scan as
    -- on site without checking the person had followed, another folded "online with no
    -- stated degree" into "complete". There is nothing to write here any more.
    count(*) FILTER (WHERE {ax.a_suivi()} AND {_EXPLOITABLE}) AS suivis,
    count(*) FILTER (WHERE {ax.n_a_pas_suivi()} AND {_EXPLOITABLE}) AS absents,
    -- Expected, and no trace of any kind. Neither an attendance nor an absence: a
    -- state of its own, so silence is visible instead of being absorbed by one of the
    -- other two or quietly dropped from the denominator.
    count(*) FILTER (WHERE {ax.sans_information()} AND {_EXPLOITABLE}) AS sans_information,
    count(*) FILTER (WHERE {ax.sur_place()} AND {_EXPLOITABLE}) AS presentiel,
    count(*) FILTER (WHERE {ax.a_distance()} AND {_EXPLOITABLE}) AS en_ligne,
    count(*) FILTER (WHERE {ax.canal_inconnu()} AND {_EXPLOITABLE}) AS suivi_modalite_inconnue,
    count(*) FILTER (WHERE {ax.prouve()} AND {_EXPLOITABLE}) AS presentiel_prouve,
    count(*) FILTER (WHERE {ax.declare()} AND {_EXPLOITABLE}) AS presentiel_declare,
    count(*) FILTER (WHERE {ax.en_ligne_entier()} AND {_EXPLOITABLE}) AS en_ligne_complet,
    count(*) FILTER (WHERE {ax.en_ligne_partiel()} AND {_EXPLOITABLE}) AS en_ligne_partiel,
    count(*) FILTER (WHERE {ax.en_ligne_sans_degre()} AND {_EXPLOITABLE}) AS en_ligne_sans_degre,
    -- Kept for the screens that still read them. "presents" no longer means anyone
    -- whose status says so: it means on site, which is what the word says.
    count(*) FILTER (WHERE {ax.sur_place()} AND {_EXPLOITABLE}) AS presents,
    count(*) FILTER (WHERE {ax.en_ligne_partiel()} AND {_EXPLOITABLE}) AS partiels,
    count(*) FILTER (WHERE {ax.prouve()} AND {_EXPLOITABLE}) AS scannes,
    count(*) FILTER (WHERE cc.ambigu) AS non_interpretables
"""


def repartition(dimension: str, filtres: dict[str, Any], role: str | None) -> list[dict[str, Any]]:
    """Attendance broken down by one axis, under the given filters."""
    expr = _expr(dimension)
    where, params = _where(filtres)
    rows = db.fetch_all(
        f"WITH {_CONSO} SELECT {expr} AS cle, {_AGREGATS} "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        f"GROUP BY {expr} ORDER BY presents DESC, cle ASC",
        params, role=role,
    )
    return [_ligne(r) for r in rows]


def tableau_croise(
    lignes: str, colonnes: str, filtres: dict[str, Any], role: str | None,
    mesure: str = "presents",
) -> dict[str, Any]:
    """A real cross-tab: one row per row-axis value, one column per column-axis value.

    Not a list of concatenated labels. The direction's question is "how does this
    commission split across modalities", and reading that off "Commission A - En
    ligne" rows means doing the pivot by eye. The matrix is built here, with its
    margins, so the screen renders a table a statistician recognises.
    """
    expr_l = _expr(lignes)
    expr_c = _expr(colonnes)
    if lignes == colonnes:
        raise DimensionInconnue("les deux axes du croisement doivent différer")
    mesures = {
        "presents": "count(*) FILTER (WHERE cc.present)",
        "participants": "count(*) FILTER (WHERE cc.present OR cc.partiel)",
        "absents": "count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel)",
        "total": "count(*)",
    }
    if mesure not in mesures:
        raise DimensionInconnue(f"mesure inconnue : {mesure}")
    where, params = _where(filtres)

    rows = db.fetch_all(
        f"WITH {_CONSO} SELECT {expr_l} AS l, {expr_c} AS c, {mesures[mesure]} AS v, count(*) AS n "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        f"GROUP BY {expr_l}, {expr_c}",
        params, role=role,
    )

    # Margins are computed from the same rows rather than by a second query: a total
    # that does not equal the sum of its cells is the fastest way to lose a reader.
    cellules: dict[tuple[str, str], int] = {}
    total_ligne: dict[str, int] = {}
    total_colonne: dict[str, int] = {}
    for r in rows:
        cle_l = r["l"] or _INCONNU
        cle_c = r["c"] or _INCONNU
        v = int(r["v"] or 0)
        cellules[(cle_l, cle_c)] = v
        total_ligne[cle_l] = total_ligne.get(cle_l, 0) + v
        total_colonne[cle_c] = total_colonne.get(cle_c, 0) + v

    ordre_l = [k for k, _ in sorted(total_ligne.items(), key=lambda kv: (-kv[1], kv[0]))]
    ordre_c = [k for k, _ in sorted(total_colonne.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {
        "mesure": mesure,
        "axe_lignes": {"cle": lignes, "libelle": DIMENSIONS[lignes]["libelle"], "valeurs": ordre_l},
        "axe_colonnes": {"cle": colonnes, "libelle": DIMENSIONS[colonnes]["libelle"], "valeurs": ordre_c},
        "cellules": [
            {"ligne": lig, "colonne": col, "valeur": cellules.get((lig, col), 0)}
            for lig in ordre_l for col in ordre_c
        ],
        "totaux_lignes": [{"label": lig, "valeur": total_ligne[lig]} for lig in ordre_l],
        "totaux_colonnes": [{"label": c, "valeur": total_colonne[c]} for c in ordre_c],
        "total_general": sum(total_ligne.values()),
    }
