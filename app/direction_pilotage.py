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

from . import axes_suivi as ax
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
        f"count(*) FILTER (WHERE {ax.a_suivi()} AND {_EXPLOITABLE}) AS suivis, "
        f"count(*) FILTER (WHERE {ax.n_a_pas_suivi()} AND {_EXPLOITABLE}) AS absents, "
        f"count(*) FILTER (WHERE {ax.sans_information()} AND {_EXPLOITABLE}) AS sans_information, "
        f"count(*) FILTER (WHERE {ax.sur_place()} AND {_EXPLOITABLE}) AS presentiel, "
        f"count(*) FILTER (WHERE {ax.a_distance()} AND {_EXPLOITABLE}) AS en_ligne, "
        f"count(*) FILTER (WHERE {ax.canal_inconnu()} AND {_EXPLOITABLE}) AS canal_inconnu, "
        f"count(*) FILTER (WHERE {ax.prouve()} AND {_EXPLOITABLE}) AS scannes, "
        # Level three: how complete the remote attendance was. Computed over the people
        # who attended remotely and nobody else, because a partial follow-up does not
        # exist on site: somebody who came for half an hour came.
        f"count(*) FILTER (WHERE {ax.en_ligne_entier()} AND {_EXPLOITABLE}) AS complet, "
        f"count(*) FILTER (WHERE {ax.en_ligne_partiel()} AND {_EXPLOITABLE}) AS partiels, "
        f"count(*) FILTER (WHERE {ax.en_ligne_sans_degre()} AND {_EXPLOITABLE}) AS distanciel_sans_degre "
        f"FROM conso cc {_JOINTURES} WHERE {where} "
        "GROUP BY e.id, e.titre, e.debut, e.volet ORDER BY e.debut",
        params, role=role,
    )
    serie = []
    for r in rows:
        suivis = int(r["suivis"] or 0)
        absents = int(r["absents"] or 0)
        sans_info = int(r["sans_information"] or 0)
        presentiel = int(r["presentiel"] or 0)
        en_ligne = int(r["en_ligne"] or 0)
        # The whole audience the activity concerned. It stopped at suivis + absents,
        # which is the count of people who answered: an activity where five people
        # replied out of seventy showed a rate computed on five.
        total = suivis + absents + sans_info
        serie.append({
            "evenement_id": str(r["id"]),
            "titre": r["titre"],
            "date": r["debut"].isoformat() if r["debut"] else None,
            "volet": r["volet"],
            "suivis": suivis,
            "presentiel": presentiel,
            "en_ligne": en_ligne,
            "canal_inconnu": int(r["canal_inconnu"] or 0),
            "sans_information": sans_info,
            "repondants": suivis + absents,
            # Level three, over the remote attendees only.
            "complet": int(r["complet"] or 0),
            "distanciel_sans_degre": int(r["distanciel_sans_degre"] or 0),
            # "presents" holds the on-site count, which is what the word means. It used
            # to hold everyone whose status said they had followed, online included,
            # so the lower curve of the trend chart was not the one it was labelled.
            "presents": presentiel,
            "partiels": int(r["partiels"] or 0),
            "absents": absents,
            "total": total,
            "scannes": int(r["scannes"] or 0),
            "taux_presence": _taux(presentiel, total),
            "taux_suivi": _taux(suivis, total),
            "taux_participation": _taux(suivis, total),
            "taux_reponse": _taux(suivis + absents, total),
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
    """Assiduity per member, over the occasions each was actually expected at.

    The rate used to divide by the number of activities for which a record happened to
    exist, which measures answering rather than attending and rewards silence: a member
    with one record who followed it read as one hundred percent while ignoring twenty
    eight activities they were expected at. Fifty of seventy two members moved by ten
    points or more once the denominator became eligibility, and eleven members who never
    left a trace stopped being invisible.

    The whole method lives in :mod:`assiduite`, with its exposure floor, its confidence
    interval and its separation of excused from unexcused, so no screen can quietly
    apply a different one.
    """
    from . import assiduite

    where, params = _where(filtres)
    return assiduite.cohortes(filtres, role, where, params)

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
    # Every count comes from the shared vocabulary. These were hand-written here, and
    # they had already drifted: "presents" counted the status, which the database sets
    # for anybody who followed by any means, so people who never left home were
    # counted present.
    r = db.fetch_one(
        f"WITH {_CONSO} SELECT "
        f"count(*) FILTER (WHERE {_EXPLOITABLE}) AS attendues, "
        f"count(*) FILTER (WHERE {ax.a_repondu()} AND {_EXPLOITABLE}) AS repondants, "
        f"count(DISTINCT cc.membre_id) FILTER (WHERE {ax.a_repondu()} AND {_EXPLOITABLE}) AS membres_vus, "
        f"count(DISTINCT cc.evenement_id) FILTER (WHERE {_EXPLOITABLE}) AS activites, "
        f"count(*) FILTER (WHERE {ax.a_suivi()} AND {_EXPLOITABLE}) AS suivis, "
        f"count(*) FILTER (WHERE {ax.n_a_pas_suivi()} AND {_EXPLOITABLE}) AS absents, "
        f"count(*) FILTER (WHERE {ax.sans_information()} AND {_EXPLOITABLE}) AS sans_information, "
        f"count(*) FILTER (WHERE {ax.sur_place()} AND {_EXPLOITABLE}) AS presentiel, "
        f"count(*) FILTER (WHERE {ax.a_distance()} AND {_EXPLOITABLE}) AS en_ligne, "
        f"count(*) FILTER (WHERE {ax.prouve()} AND {_EXPLOITABLE}) AS presentiel_prouve, "
        f"count(*) FILTER (WHERE {ax.declare()} AND {_EXPLOITABLE}) AS presentiel_declare, "
        f"count(*) FILTER (WHERE {ax.en_ligne_entier()} AND {_EXPLOITABLE}) AS en_ligne_complet, "
        f"count(*) FILTER (WHERE {ax.en_ligne_partiel()} AND {_EXPLOITABLE}) AS en_ligne_partiel, "
        f"count(*) FILTER (WHERE {ax.en_ligne_sans_degre()} AND {_EXPLOITABLE}) AS en_ligne_sans_degre, "
        f"count(*) FILTER (WHERE {ax.canal_inconnu()} AND {_EXPLOITABLE}) AS suivi_modalite_inconnue, "
        "count(*) FILTER (WHERE cc.ambigu) AS non_interpretables "
        f"FROM conso cc {_JOINTURES} WHERE {where}",
        params, role=role,
    ) or {}

    membres_actifs = _effectif_du_perimetre(filtres, role)

    n = lambda k: int(r.get(k) or 0)  # noqa: E731 - one short reader, used a dozen times
    # The denominator of every rate below: everybody the activities concerned. Not the
    # people who answered. The two differ by a third of the base here, and the missing
    # third attends least, so the narrower denominator flattered every figure.
    attendues = n("attendues")
    repondants = n("repondants")
    sans_info = n("sans_information")
    suivis = n("suivis")
    activites = n("activites")
    prouve = n("presentiel_prouve")
    presentiel = n("presentiel")
    en_ligne = n("en_ligne")
    return {
        # Kept under its old name because screens read it; it now holds the expected
        # audience rather than the answer count, which is what the word implies.
        "observations": attendues,
        "attendues": attendues,
        "repondants": repondants,
        "sans_information": sans_info,
        "activites": activites,
        "membres_vus": n("membres_vus"),
        "membres_actifs": int(membres_actifs or 0),
        # "presents" means on site, which is what the word means.
        "presents": presentiel,
        "partiels": n("en_ligne_partiel"),
        "absents": n("absents"),
        "scannes": prouve,
        # The four ways of having followed. They sum to `suivis`, so the breakdown can
        # be checked against the headline rather than trusted.
        "presentiel_prouve": prouve,
        "presentiel_declare": n("presentiel_declare"),
        "en_ligne_complet": n("en_ligne_complet"),
        "en_ligne_partiel": n("en_ligne_partiel"),
        "en_ligne_sans_degre": n("en_ligne_sans_degre"),
        "suivi_modalite_inconnue": n("suivi_modalite_inconnue"),
        "suivis": suivis,
        "non_interpretables": n("non_interpretables"),
        "presentiel": presentiel,
        "en_ligne": en_ligne,
        "taux_presence": _taux(presentiel, attendues),
        "taux_presence_physique": _taux(presentiel, attendues),
        "taux_suivi": _taux(suivis, attendues),
        "taux_participation": _taux(suivis, attendues),
        "taux_a_distance": _taux(en_ligne, attendues),
        # Level two rates: over the people who attended, not over everybody. Asking
        # "of those who came, how many came on site" is a different question from
        # "of everybody expected", and mixing the two denominators on one screen is
        # what made the mode split unreadable.
        "part_presentiel": _taux(presentiel, suivis),
        "part_distanciel": _taux(en_ligne, suivis),
        "taux_absence": _taux(n("absents"), attendues),
        "taux_sans_information": _taux(sans_info, attendues),
        # How often the platform gets an answer at all. It qualifies everything above:
        # a low response rate does not make the attendance figures wrong, it makes the
        # unknown share large.
        "taux_reponse": _taux(repondants, attendues),
        # What share of the attendance is evidence rather than assertion. Eighty per
        # cent proven at the door and eighty per cent asserted in a form are different
        # situations, and the attendance rate alone cannot tell them apart.
        "taux_preuve": _taux(prouve, suivis),
        # How much of the base the figures above actually describe.
        "taux_couverture": _taux(n("membres_vus"), int(membres_actifs or 0)),
        # People who followed, per activity. It counted only those on site, under a
        # label that said "suivis".
        "moyenne_par_activite": round(suivis / activites, 1) if activites else 0.0,
    }


def regles_calcul(filtres: dict[str, Any], role: str | None) -> dict[str, Any]:
    """Every published figure with its definition, its formula and its arithmetic check.

    The same filters as every other panel, applied by the same builder: a transparency
    table that described a different selection than the dashboard beside it would be
    worse than no table at all.
    """
    from . import indicateurs

    where, params = _where(filtres)
    resultat = indicateurs.calculer(where, params)
    # The organisational reach is not a count of rows and cannot come from the same
    # pass, but a reader comparing the table to the dashboard will look for it.
    resultat["membres_du_perimetre"] = int(_effectif_du_perimetre(filtres, role) or 0)
    return resultat


def arrivees(evenement_id: str, role: str | None) -> dict[str, Any]:
    """When people actually arrived at one activity, from the recorded times.

    This replaces a simulated curve. The component drew a Gaussian calibrated on the
    total and labelled it "indicative", which is a fabricated shape presented as a
    measurement: a reader saw a smooth arrival flow that nothing had observed, and the
    peak sat at fifty five percent of the slot because that is where the formula put it.

    The times exist and always did (``presence.arrivee``, filled on every one of the
    five hundred and eleven rows). Where they carry no spread, because a seeder wrote
    them all at the start, the answer says so instead of drawing a curve over it.
    """
    ev = db.fetch_one("SELECT debut, fin FROM evenement WHERE id = %s", (evenement_id,), role=role)
    if not ev or not ev["debut"]:
        return {"disponible": False, "motif": "L'activité n'a pas d'heure de début enregistrée.", "tranches": []}

    lignes = db.fetch_all(
        "SELECT floor(extract(epoch FROM (p.arrivee - e.debut)) / 900)::int AS quart, count(*) AS n "
        "FROM presence p JOIN evenement e ON e.id = p.evenement_id "
        "WHERE p.evenement_id = %s AND p.arrivee IS NOT NULL "
        "GROUP BY 1 ORDER BY 1",
        (evenement_id,),
        role=role,
    )
    if not lignes:
        return {"disponible": False, "motif": "Aucun pointage horodaté pour cette activité.", "tranches": []}

    total = sum(int(r["n"]) for r in lignes)
    cumul = 0
    tranches = []
    for r in lignes:
        cumul += int(r["n"])
        minutes = int(r["quart"]) * 15
        tranches.append({
            "minutes": minutes,
            "libelle": f"{minutes:+d} min" if minutes else "à l'heure",
            "arrivees": int(r["n"]),
            "cumul": cumul,
            "part_cumulee": round(100.0 * cumul / total, 1) if total else 0.0,
        })
    # One bucket means every recorded arrival shares the same quarter hour. A curve over
    # a single point is a line drawn between one value and itself.
    etale = len(tranches) > 1
    return {
        "disponible": etale,
        "motif": None if etale else (
            "Tous les pointages portent le même horodatage, celui du début de l'activité. "
            "Il n'y a pas d'étalement à représenter : le contrôle a enregistré les passages "
            "sans horaire distinct."
        ),
        "total": total,
        "tranches": tranches,
    }
