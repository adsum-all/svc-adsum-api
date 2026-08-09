"""Assiduity, measured the way the discipline measures it.

The previous rate divided what a member followed by the number of activities for which
a record happened to exist. That measures answering behaviour, not attendance, and it
rewards silence: a member with one record who followed it showed 100 percent while
ignoring twenty eight other activities they were expected at. Fifty of seventy two
members moved by ten points or more once the denominator was fixed, and eleven members
who were expected and never left a trace did not appear at all.

What the measurement of attendance actually requires, and what is implemented here.

**The denominator is exposure, not response.** It counts the occasions a person was
eligible for: activities that targeted them, already past, not cancelled, and starting
after they joined. Somebody enrolled in June is not absent in March. This is the
"enrolled occasions" of education research and the exposure denominator of
epidemiology, and getting it wrong is the classic way an attendance figure flatters.

**An aggregate rate and a chronic-absence count are two different indicators.** A body
can average eighty percent while a fifth of its members almost never come. The average
describes the organisation, the banding describes its people, and only the second tells
anyone who to call. Both are published.

**Below a floor of exposure, a rate is noise.** Someone eligible twice who came once is
at fifty percent, statistically indistinguishable from thirty or seventy. Those members
are set apart rather than banded, and every rate carries a Wilson score interval so a
reader sees how much the figure can be trusted. Wilson rather than the textbook normal
approximation because the latter is badly behaved near zero and one hundred, which is
exactly where the decisions are made.

**Excused and unexcused are not the same absence.** Illness is not disengagement.
Aggregating them produces a number nobody can act on, so the two are counted apart,
using the qualification a habilitated responsible has recorded, never the member's own
declaration.

**A streak says more than a rate.** Someone at fifty percent who is coming back differs
from someone at fifty percent who stopped three months ago. The count of consecutive
missed activities, most recent first, is the strongest early signal there is.
"""
# ruff: noqa: E501
from __future__ import annotations

import math
from typing import Any

from . import db
from .direction_analyse import _CONSO, _EXPLOITABLE
from .visibilite import CIBLE_PREDICATE

#: Below this many eligible occasions, a percentage carries no information worth banding.
#: Ten is the usual floor in attendance research; it is a setting rather than a constant
#: because a body meeting twice a year would never reach it.
EXPOSITION_MINIMALE = 10

#: Bands over the eligible-occasion rate. The lowest one matches the definition of
#: chronic absence used in the literature: missing more than a tenth is the alert, and
#: everything under twenty percent followed is disengagement rather than irregularity.
BANDES: tuple[tuple[str, str, float, float], ...] = (
    ("assidu", "Assidus, 80 % et plus", 80.0, 100.01),
    ("regulier", "Réguliers, 50 à 79 %", 50.0, 80.0),
    ("irregulier", "Irréguliers, 20 à 49 %", 20.0, 50.0),
    ("decroche", "Décrochés, moins de 20 %", -0.01, 20.0),
)


def intervalle_wilson(succes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """The 95 percent Wilson score interval for a proportion, in percent.

    Preferred to the normal approximation because that one produces bounds below zero
    or above one hundred exactly where attendance figures live, and because it collapses
    to a useless zero-width interval when nobody attended.
    """
    if total <= 0:
        return (0.0, 100.0)
    p = succes / total
    denominateur = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominateur
    ecart = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominateur
    return (round(100 * max(0.0, centre - ecart), 1), round(100 * min(1.0, centre + ecart), 1))


def _sql_par_membre(where: str) -> str:
    """Per member: what they were eligible for, and what they did with it."""
    return f"""
WITH {_CONSO},
attendu AS (
    SELECT m.id AS membre_id,
           coalesce(nullif(btrim(concat_ws(' ', m.nom, m.prenoms)), ''), m.matricule) AS nom,
           m.matricule,
           count(*) AS eligibles
    FROM membre m
    LEFT JOIN intendance mi ON mi.id = m.intendance_id
    JOIN evenement e
      ON NOT e.annule
     AND e.debut < now()
     AND {CIBLE_PREDICATE}
     -- Nobody is absent from an activity that happened before they joined.
     AND e.debut >= coalesce(m.date_entree::timestamptz, m.cree_le, '-infinity'::timestamptz)
    WHERE m.statut = 'actif'
    GROUP BY m.id, m.nom, m.prenoms, m.matricule
),
fait AS (
    SELECT cc.membre_id,
           count(*) FILTER (WHERE (cc.present OR cc.partiel) AND {_EXPLOITABLE}) AS suivies,
           count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel
                              AND cc.absence_qualification = 'excusee' AND {_EXPLOITABLE}) AS excusees,
           count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel
                              AND cc.absence_qualification <> 'excusee' AND {_EXPLOITABLE}) AS non_excusees,
           count(*) FILTER (WHERE {_EXPLOITABLE}) AS avec_trace
    FROM conso cc
    WHERE {where}
    GROUP BY cc.membre_id
)
SELECT a.membre_id, a.nom, a.matricule, a.eligibles,
       coalesce(f.suivies, 0) AS suivies,
       coalesce(f.excusees, 0) AS excusees,
       coalesce(f.non_excusees, 0) AS non_excusees,
       coalesce(f.avec_trace, 0) AS avec_trace
FROM attendu a LEFT JOIN fait f ON f.membre_id = a.membre_id
ORDER BY a.eligibles DESC
"""


def cohortes(filtres: dict[str, Any], role: str | None, where: str, params: tuple[Any, ...] | dict[str, Any]) -> dict[str, Any]:
    """Assiduity per member, banded, with everything needed to challenge the figure."""
    lignes = db.fetch_all(_sql_par_membre(where), params, role=role)

    compte = {cle: 0 for cle, _, _, _ in BANDES}
    exposition_faible = 0
    jamais_vus = 0
    taux: list[float] = []
    total_eligibles = 0
    total_suivies = 0
    total_excusees = 0
    total_non_excusees = 0
    sans_reponse = 0
    hors_cible = 0

    for r in lignes:
        eligibles = int(r["eligibles"] or 0)
        suivies = int(r["suivies"] or 0)
        total_eligibles += eligibles
        total_suivies += suivies
        total_excusees += int(r["excusees"] or 0)
        total_non_excusees += int(r["non_excusees"] or 0)
        # Eligible, and nothing recorded at all: neither a follow nor a declared absence.
        # These were invisible before, which is precisely why they matter.
        sans_reponse += max(0, eligibles - int(r["avec_trace"] or 0))
        if int(r["avec_trace"] or 0) == 0:
            jamais_vus += 1
        if eligibles < EXPOSITION_MINIMALE:
            exposition_faible += 1
            continue
        brut = 100.0 * suivies / eligibles
        # Above one hundred means the person followed an activity they were not
        # expected at, or one predating their recorded entry. It is a data-quality
        # signal about the targeting or the entry date, not an attendance figure, and
        # it is reported rather than hidden. Banding uses the capped value, because
        # somebody who attended everything and more belongs at the top, not nowhere:
        # falling through every band silently dropped six members from the totals.
        if brut > 100.0:
            hors_cible += 1
        t = min(100.0, brut)
        taux.append(t)
        for cle, _, bas, haut in BANDES:
            if bas <= t < haut:
                compte[cle] += 1
                break

    classes = len(taux)
    ordonnes = sorted(taux)
    mediane = None
    if ordonnes:
        milieu = len(ordonnes) // 2
        mediane = round(ordonnes[milieu] if len(ordonnes) % 2 else (ordonnes[milieu - 1] + ordonnes[milieu]) / 2, 1)

    bas, haut = intervalle_wilson(total_suivies, total_eligibles)
    return {
        "methode": {
            "denominateur": "Les activités auxquelles le membre était attendu : passées, non annulées, qui le ciblaient, et postérieures à son entrée.",
            "exposition_minimale": EXPOSITION_MINIMALE,
            "note_exposition": f"Un membre attendu moins de {EXPOSITION_MINIMALE} fois n'est pas classé : sur si peu d'occasions, un pourcentage ne distingue rien.",
            "intervalle": "Intervalle de Wilson à 95 %, qui reste dans les bornes 0 et 100 là où l'approximation usuelle en sort.",
        },
        "membres_evalues": classes,
        "membres_exposition_faible": exposition_faible,
        "membres_jamais_vus": jamais_vus,
        "membres_suivi_hors_cible": hors_cible,
        "note_hors_cible": (
            "Membres ayant suivi plus d'activités qu'ils n'en étaient attendus : "
            "ils ont suivi une activité qui ne les ciblait pas, ou antérieure à leur date "
            "d'entrée enregistrée. Leur taux est plafonné à 100 % et le cas est signalé, "
            "car il révèle une incohérence de ciblage ou de date, pas une assiduité."
        ),
        "occasions_eligibles": total_eligibles,
        "occasions_suivies": total_suivies,
        "occasions_sans_reponse": sans_reponse,
        "absences_excusees": total_excusees,
        "absences_non_excusees": total_non_excusees,
        "taux_global": round(100.0 * total_suivies / total_eligibles, 1) if total_eligibles else None,
        "taux_global_intervalle": {"bas": bas, "haut": haut},
        "mediane": mediane,
        # The previous field names, kept so the Direction screen keeps rendering while
        # it is updated. Same values, clearer names beside them: a rename that breaks a
        # live dashboard to gain a word is a bad trade.
        "membres_classes": classes,
        "membres_donnees_insuffisantes": exposition_faible,
        "taux_median": mediane if mediane is not None else 0.0,
        "taux_moyen": round(sum(taux) / len(taux), 1) if taux else 0.0,
        # The share of people in the lowest band. An average cannot show this, and it is
        # the figure a leader acts on.
        "part_decroches": round(100.0 * compte["decroche"] / classes, 1) if classes else None,
        "cohortes": [
            {
                "cle": cle,
                "label": label,
                "membres": compte[cle],
                "part": round(100.0 * compte[cle] / classes, 1) if classes else None,
            }
            for cle, label, _, _ in BANDES
        ],
    }
