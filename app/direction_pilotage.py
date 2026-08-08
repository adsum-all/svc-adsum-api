# ruff: noqa: E501 - the aggregate expressions read better on one line each
"""Steering views for the direction: drill-down, assiduity cohorts, trends.

A breakdown says where people are. Steering asks the next questions: which unit is
slipping, who stopped coming, is this month worse than the last. Those need the
organisation read as a tree rather than a flat list, attendance read per member
rather than per row, and time read as a series rather than a total.

Everything is cut from the same consolidation as
:mod:`app.direction_analyse`, so a rate here and a rate there are the same
arithmetic on the same rows. Filters are the ones declared there too: a screen sets
its filters once and every panel on it narrows together.

The honesty rules that matter most here.

A rate is reported with the volume behind it. Three activities give a percentage that
looks exactly like one computed over three hundred, and only one of them means
anything. Every rate travels with its denominator so the screen can qualify it.

A member with no record is not an absentee. Attendance is only counted where the
platform holds a fact; the response rate is reported separately so the direction can
see how much of the picture is actually covered.
"""
from __future__ import annotations

from typing import Any

from . import db
from .direction_analyse import _CONSO, _EXPLOITABLE, _FILTRES_ENUMERES, _JOINTURES, _where

_INCONNU = "Non renseigné"


def _taux(n: int, base: int) -> float:
    return round(100.0 * n / base, 1) if base else 0.0


def arborescence(filtres: dict[str, Any], role: str | None) -> list[dict[str, Any]]:
    """The organisation as a tree, with attendance measured at every node.

    Coordination, then its stewardships, then the commissions of the members inside
    them. This is the descent the direction asked for: seeing a coordination at
    sixty per cent means nothing until you can open it and find which stewardship
    carries the drop.

    Built from one query rather than one per node. A node whose children are fetched
    separately can disagree with its own total the moment a filter changes, and the
    reader has no way to tell which figure is the wrong one.
    """
    where, params = _where(filtres)
    rows = db.fetch_all(
        f"WITH {_CONSO} SELECT "
        f"coalesce(co.nom, cod.nom, '{_INCONNU}') AS coordination, "
        f"coalesce(i.nom, '{_INCONNU}') AS intendance, "
        f"coalesce(c.nom, '{_INCONNU}') AS commission, "
        f"count(*) FILTER (WHERE cc.present AND {_EXPLOITABLE}) AS presents, "
        f"count(*) FILTER (WHERE cc.partiel AND NOT cc.present AND {_EXPLOITABLE}) AS partiels, "
        f"count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel AND {_EXPLOITABLE}) AS absents, "
        f"count(DISTINCT cc.membre_id) FILTER (WHERE {_EXPLOITABLE}) AS membres "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        "GROUP BY 1, 2, 3",
        params, role=role,
    )

    # Aggregated in Python from the leaf rows so a parent is always exactly the sum of
    # its children. Re-querying per level is what lets totals drift apart.
    arbre: dict[str, dict[str, Any]] = {}
    for r in rows:
        co = str(r["coordination"])
        it = str(r["intendance"])
        cm = str(r["commission"])
        p, pa, ab = int(r["presents"] or 0), int(r["partiels"] or 0), int(r["absents"] or 0)
        mb = int(r["membres"] or 0)

        def _neuf(avec_enfants: bool) -> dict[str, Any]:
            vide: dict[str, Any] = {"presents": 0, "partiels": 0, "absents": 0, "membres": 0}
            if avec_enfants:
                vide["enfants"] = {}
            return vide

        noeud_co = arbre.setdefault(co, _neuf(True))
        noeud_it = noeud_co["enfants"].setdefault(it, _neuf(True))
        noeud_cm = noeud_it["enfants"].setdefault(cm, _neuf(False))
        for noeud in (noeud_co, noeud_it, noeud_cm):
            noeud["presents"] += p
            noeud["partiels"] += pa
            noeud["absents"] += ab
            noeud["membres"] += mb

    def _formater(nom: str, n: dict[str, Any], niveau: str) -> dict[str, Any]:
        total = n["presents"] + n["partiels"] + n["absents"]
        sortie: dict[str, Any] = {
            "label": nom,
            "niveau": niveau,
            "presents": n["presents"],
            "partiels": n["partiels"],
            "absents": n["absents"],
            "total": total,
            "membres": n["membres"],
            "taux_presence": _taux(n["presents"], total),
            "taux_participation": _taux(n["presents"] + n["partiels"], total),
        }
        enfants = n.get("enfants")
        if enfants:
            niveau_enfant = "intendance" if niveau == "coordination" else "commission"
            sortie["enfants"] = sorted(
                (_formater(k, v, niveau_enfant) for k, v in enfants.items()),
                key=lambda e: (-e["total"], e["label"]),
            )
        return sortie

    return sorted(
        (_formater(k, v, "coordination") for k, v in arbre.items()),
        key=lambda e: (-e["total"], e["label"]),
    )


def serie_temporelle(filtres: dict[str, Any], role: str | None) -> list[dict[str, Any]]:
    """Attendance activity by activity, in chronological order.

    One point per activity rather than per month: an assembly and a weekly meeting in
    the same month average into a figure describing neither. The activity's name and
    date travel with the point so the curve can be read rather than merely admired.
    """
    where, params = _where(filtres)
    rows = db.fetch_all(
        f"WITH {_CONSO} SELECT e.id, e.titre, e.debut, "
        f"coalesce(e.volet, '{_INCONNU}') AS volet, "
        f"count(*) FILTER (WHERE cc.present AND {_EXPLOITABLE}) AS presents, "
        f"count(*) FILTER (WHERE cc.partiel AND NOT cc.present AND {_EXPLOITABLE}) AS partiels, "
        f"count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel AND {_EXPLOITABLE}) AS absents, "
        f"count(*) FILTER (WHERE cc.scanne AND {_EXPLOITABLE}) AS scannes, "
        "count(*) FILTER (WHERE (cc.present OR cc.partiel) AND cc.a_enligne "
        "AND NOT cc.a_presentiel AND NOT cc.scanne) AS en_ligne "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        "GROUP BY e.id, e.titre, e.debut, e.volet ORDER BY e.debut",
        params, role=role,
    )
    serie = []
    for r in rows:
        p, pa, ab = int(r["presents"] or 0), int(r["partiels"] or 0), int(r["absents"] or 0)
        total = p + pa + ab
        serie.append({
            "evenement_id": str(r["id"]),
            "titre": r["titre"],
            "date": r["debut"].isoformat() if r["debut"] else None,
            "volet": r["volet"],
            "presents": p,
            "partiels": pa,
            "absents": ab,
            "total": total,
            "scannes": int(r["scannes"] or 0),
            "en_ligne": int(r["en_ligne"] or 0),
            "taux_presence": _taux(p, total),
            "taux_participation": _taux(p + pa, total),
        })
    return serie


#: Assiduity bands. Named rather than numbered because the direction acts on them:
#: a member who has stopped coming is a different conversation from one who comes
#: irregularly, and a band called "0 to 25" invites nobody to have either.
_BANDES = (
    ("assidu", "Assidus (80 % et plus)", 80.0, 100.01),
    ("regulier", "Réguliers (50 à 79 %)", 50.0, 80.0),
    ("irregulier", "Irréguliers (20 à 49 %)", 20.0, 50.0),
    ("decroche", "Décrochés (moins de 20 %)", -0.01, 20.0),
)


def cohortes_assiduite(filtres: dict[str, Any], role: str | None) -> dict[str, Any]:
    """Members grouped by how often they actually attend.

    Measured per member across the filtered activities, not as a global average: an
    overall seventy per cent hides whether seven people in ten attend everything or
    everybody attends seven times in ten. Those are different organisations and call
    for different action.

    Members with fewer than two records are set apart rather than banded. Putting
    somebody in "decrochés" on the strength of a single absence is a judgement the
    data does not support, and it is the kind of figure a leader would act on.
    """
    where, params = _where(filtres)
    rows = db.fetch_all(
        f"WITH {_CONSO} SELECT cc.membre_id, "
        f"count(*) FILTER (WHERE {_EXPLOITABLE}) AS observees, "
        f"count(*) FILTER (WHERE (cc.present OR cc.partiel) AND {_EXPLOITABLE}) AS suivies "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        "GROUP BY cc.membre_id",
        params, role=role,
    )

    compte = {cle: 0 for cle, _, _, _ in _BANDES}
    insuffisant = 0
    taux_membres: list[float] = []
    for r in rows:
        observees = int(r["observees"] or 0)
        if observees < 2:
            insuffisant += 1
            continue
        taux = 100.0 * int(r["suivies"] or 0) / observees
        taux_membres.append(taux)
        for cle, _, bas, haut in _BANDES:
            if bas <= taux < haut:
                compte[cle] += 1
                break

    classes = sum(compte.values())
    ordonnes = sorted(taux_membres)
    mediane = 0.0
    if ordonnes:
        milieu = len(ordonnes) // 2
        mediane = (
            ordonnes[milieu] if len(ordonnes) % 2
            else (ordonnes[milieu - 1] + ordonnes[milieu]) / 2
        )
    return {
        "cohortes": [
            {
                "cle": cle,
                "label": libelle,
                "membres": compte[cle],
                "part": _taux(compte[cle], classes),
            }
            for cle, libelle, _, _ in _BANDES
        ],
        "membres_classes": classes,
        "membres_donnees_insuffisantes": insuffisant,
        "taux_moyen": round(sum(taux_membres) / len(taux_membres), 1) if taux_membres else 0.0,
        "taux_median": round(mediane, 1),
    }


#: Filters that describe the PEOPLE rather than the activities. Only these narrow the
#: coverage denominator: restricting to one quarter does not make the organisation
#: smaller, but restricting to one coordination does.
_FILTRES_EFFECTIF: dict[str, str] = {
    "coordination": "coalesce(i.coordination_id, m.coordination_id) = %(f_coordination)s",
    "intendance": "m.intendance_id = %(f_intendance)s",
    "commission": "m.commission_id = %(f_commission)s",
    "tribu": "m.tribu_id = %(f_tribu)s",
    "pays": "m.pays = %(f_pays)s",
    "genre": "m.genre = %(f_genre)s",
    "type_membre": "m.type_membre = %(f_type_membre)s",
}


def _effectif_du_perimetre(filtres: dict[str, Any], role: str | None) -> int:
    """How many active members the current organisational scope contains.

    Counted under the same organisational filters as the numerator. Left global, the
    coverage rate collapsed the moment anybody narrowed: seven observed members in a
    coordination against sixty-four active in the whole organisation reads as eleven
    per cent, and a leader would conclude their coordination reports almost nothing.

    Period, volet and activity filters are deliberately ignored here. Looking at one
    quarter does not reduce the number of people the organisation has, and letting it
    would make coverage rise simply because the window narrowed.
    """
    morceaux: list[str] = []
    params: dict[str, Any] = {}
    for cle, valeur in filtres.items():
        if valeur in (None, ""):
            continue
        # The demonstration switch describes people, so it narrows the denominator
        # too. Excluded, coverage would compare real observed members against a
        # population that still counted the demonstration profiles.
        if cle == "donnees":
            choix = _FILTRES_ENUMERES.get("donnees", {}).get(str(valeur))
            if choix is not None:
                morceaux.append(choix)
            continue
        fragment = _FILTRES_EFFECTIF.get(cle)
        if fragment is None:
            continue
        morceaux.append(fragment)
        params[f"f_{cle}"] = valeur
    where = " AND ".join(morceaux) if morceaux else "true"

    r = db.fetch_one(
        "SELECT count(*) AS n FROM membre m "
        "LEFT JOIN intendance i ON i.id = m.intendance_id "
        "WHERE m.statut = 'actif' AND m.statut_inscription = 'approuve' "
        f"AND {where}",
        params, role=role,
    )
    return int((r or {}).get("n", 0) or 0)


def synthese(filtres: dict[str, Any], role: str | None) -> dict[str, Any]:
    """The headline figures, computed from the same rows as every panel below them.

    A dashboard whose cards are queried separately from its charts will eventually
    show a card that contradicts the chart under it. These come from the
    consolidation, so they cannot.

    The response rate is reported because it qualifies everything else: a
    ninety-per-cent attendance rate measured on a third of the base is a statement
    about that third.
    """
    where, params = _where(filtres)
    # Every count here carries the same exclusion as the breakdowns below it. It did
    # not, and the headline therefore counted the hundred and eleven uninterpretable
    # rows while the charts under it did not: the four buckets no longer added up to
    # the number of people the card said had followed.
    r = db.fetch_one(
        f"WITH {_CONSO} SELECT "
        f"count(*) FILTER (WHERE {_EXPLOITABLE}) AS observations, "
        f"count(DISTINCT cc.membre_id) FILTER (WHERE {_EXPLOITABLE}) AS membres_vus, "
        f"count(DISTINCT cc.evenement_id) FILTER (WHERE {_EXPLOITABLE}) AS activites, "
        f"count(*) FILTER (WHERE cc.present AND {_EXPLOITABLE}) AS presents, "
        f"count(*) FILTER (WHERE cc.partiel AND NOT cc.present AND {_EXPLOITABLE}) AS partiels, "
        f"count(*) FILTER (WHERE cc.absent AND NOT cc.present AND NOT cc.partiel AND {_EXPLOITABLE}) AS absents, "
        f"count(*) FILTER (WHERE cc.scanne AND {_EXPLOITABLE}) AS presentiel_prouve, "
        f"count(*) FILTER (WHERE (cc.present OR cc.partiel) AND cc.a_presentiel AND NOT cc.scanne AND {_EXPLOITABLE}) AS presentiel_declare, "
        f"count(*) FILTER (WHERE cc.a_enligne AND NOT cc.en_ligne_partiel AND NOT cc.a_presentiel AND NOT cc.scanne AND {_EXPLOITABLE}) AS en_ligne_complet, "
        f"count(*) FILTER (WHERE cc.a_enligne AND cc.en_ligne_partiel AND NOT cc.a_presentiel AND NOT cc.scanne AND {_EXPLOITABLE}) AS en_ligne_partiel, "
        f"count(*) FILTER (WHERE (cc.present OR cc.partiel) AND NOT cc.a_presentiel AND NOT cc.a_enligne AND NOT cc.scanne AND {_EXPLOITABLE}) AS suivi_modalite_inconnue, "
        "count(*) FILTER (WHERE cc.ambigu) AS non_interpretables "
        f"FROM conso cc {_JOINTURES} WHERE {where}",
        params, role=role,
    ) or {}

    membres_actifs = _effectif_du_perimetre(filtres, role)

    observations = int(r.get("observations") or 0)
    presents = int(r.get("presents") or 0)
    partiels = int(r.get("partiels") or 0)
    membres_vus = int(r.get("membres_vus") or 0)
    activites = int(r.get("activites") or 0)
    prouve = int(r.get("presentiel_prouve") or 0)
    declare = int(r.get("presentiel_declare") or 0)
    complet = int(r.get("en_ligne_complet") or 0)
    partiel_en_ligne = int(r.get("en_ligne_partiel") or 0)
    modalite_inconnue = int(r.get("suivi_modalite_inconnue") or 0)
    suivis = presents + partiels
    return {
        "observations": observations,
        "activites": activites,
        "membres_vus": membres_vus,
        "membres_actifs": int(membres_actifs or 0),
        "presents": presents,
        "partiels": partiels,
        "absents": int(r.get("absents") or 0),
        "scannes": prouve,
        # The four ways of having followed. They sum to `suivis`, so the breakdown can
        # be checked against the headline rather than trusted.
        "presentiel_prouve": prouve,
        "presentiel_declare": declare,
        "en_ligne_complet": complet,
        "en_ligne_partiel": partiel_en_ligne,
        "suivi_modalite_inconnue": modalite_inconnue,
        "suivis": suivis,
        "non_interpretables": int(r.get("non_interpretables") or 0),
        "taux_presence": _taux(presents, observations),
        "taux_participation": _taux(suivis, observations),
        # What share of the attendance is evidence rather than assertion. Eighty per
        # cent proven at the door and eighty per cent asserted in a form are different
        # situations, and the attendance rate alone cannot tell them apart.
        "taux_preuve": _taux(prouve, suivis),
        # How much of the base the figures above actually describe. Without it, a rate
        # computed on a handful of people reads exactly like one computed on everybody.
        "taux_couverture": _taux(membres_vus, int(membres_actifs or 0)),
        "moyenne_par_activite": round(presents / activites, 1) if activites else 0.0,
    }
