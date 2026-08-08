"""Give the direction dashboard something true to show, without inventing people.

The dashboards read consolidated attendance broken down by organisational unit. The
base holds seventy-six members and twenty-eight activities but only thirty-two
attendance records, and the fifty-three demonstration members are attached to no unit
at all. Every breakdown therefore renders one or two bars, and the screens look
broken when they are merely empty.

This attaches the demonstration members to real units and records attendance for them
on activities that have already happened, so the charts show a distribution somebody
can read and judge.

Three rules govern everything here.

Only demonstration members are ever touched. A member is one when their family name
carries DEMO or their address is on a sample domain; the filter is applied to every
statement, so a real member's record cannot be reached by this script even by
mistake.

It is idempotent. Attendance is written with ON CONFLICT DO NOTHING on the unique
(activity, member) key, and attachments are only set where they are still empty.
Running it twice changes nothing the second time, which is what makes it safe to run
at all.

It is deterministic. The distribution comes from a seeded generator, so the same run
produces the same figures: a chart that changes on every refresh is a chart nobody
trusts, and a bug in it could never be reproduced.

Usage:
    python scripts/seed_demonstration.py --simulation      # compte, n'écrit rien
    python scripts/seed_demonstration.py --appliquer
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

#: What makes a member a demonstration member. Applied to every statement, so a real
#: member's record cannot be reached by this script even by mistake.
#:
#: The per cent signs are doubled because the driver reads a lone % as the start of a
#: placeholder: written singly, every statement here fails to parse.
#: ``translate`` folds the accent so the predicate keeps matching after the family
#: names are normalised to DEMO: without it, the very first run would rename the rows
#: out of its own filter and every later run would find nothing.
_DEMO = (
    "(translate(upper(m.nom), 'ÉÈÊË', 'EEEE') LIKE '%%DEMO%%' "
    "OR m.email LIKE '%%@exemple.com' OR m.email LIKE '%%@example.com')"
)

#: The generator is seeded so two runs describe the same organisation. A dashboard
#: whose bars move on every refresh teaches nobody anything.
_GRAINE = 20260805


def _nommer_demonstration(simulation: bool) -> int:
    """Make every demonstration profile say so in its family name.

    Ten of them carry entirely credible surnames, BOUCHARD, KOUASSI, LEFEVRE and the
    like, on reserved sample domains. On a screen there is nothing to tell them apart
    from a real member, and a leader reading an attendance list has no reason to
    suspect the person they are about to call does not exist. That is the risk this
    closes: the family name itself now says DEMO.

    What distinguished them is preserved in the given names rather than dropped, so a
    profile stays identifiable, DEMO-ALPHA / Role Admin becomes DEMO / Alpha Role
    Admin. Accented because a capital keeps its accent in French.
    """
    lignes = db.fetch_all(
        f"SELECT m.id, m.nom, m.prenoms FROM membre m WHERE {_DEMO} AND m.nom <> 'DÉMO'",
        (),
    )
    for r in lignes:
        ancien = str(r["nom"] or "").strip()
        prenoms = str(r["prenoms"] or "").strip()
        # DEMO-ALPHA keeps "Alpha"; a credible surname is simply dropped, it was never
        # anybody's name.
        suffixe = ""
        sans_accent = ancien.upper().replace("É", "E")
        if sans_accent.startswith("DEMO-"):
            suffixe = ancien[5:].replace("-", " ").title()
        nouveaux = f"{suffixe} {prenoms}".strip() if suffixe else prenoms
        if not simulation:
            db.execute(
                f"UPDATE membre m SET nom = 'DÉMO', prenoms = %s WHERE m.id = %s AND {_DEMO}",
                (nouveaux or "Profil", str(r["id"])),
            )
    return len(lignes)


def _hierarchie_intendances(simulation: bool) -> int:
    """Attach stewardships to a coordination when they have none.

    The direction screen is meant to descend from a coordination into its
    stewardships and their commissions. Almost every stewardship carries no
    coordination, so that descent currently ends at the first step.

    Ivorian stewardships go to the Africa coordination and the named ones follow
    their city; nothing is guessed beyond what the names already say.
    """
    coords = {
        str(r["nom"]): str(r["id"])
        for r in db.fetch_all("SELECT id, nom FROM coordination", ())
    }
    afrique = coords.get("Coordination AFRIQUE")
    europe = coords.get("Coordination EUROPE")
    canada = coords.get("Coordination CANADA")
    if not afrique:
        return 0

    # A stewardship whose name names a place is attached to the coordination of that
    # place; everything else belongs to the base's home coordination.
    par_nom = {"Intendance Paris Centre": europe, "Intendance Montreal Ouest": canada}

    rattaches = 0
    for r in db.fetch_all("SELECT id, nom FROM intendance WHERE coordination_id IS NULL", ()):
        cible = par_nom.get(str(r["nom"]), afrique)
        if not cible:
            continue
        rattaches += 1
        if not simulation:
            db.execute(
                "UPDATE intendance SET coordination_id = %s WHERE id = %s AND coordination_id IS NULL",
                (cible, str(r["id"])),
            )
    return rattaches


def _rattacher_membres(simulation: bool) -> dict[str, int]:
    """Give every demonstration member a commission, a tribe and a stewardship.

    Spread deterministically rather than evenly: a perfectly flat distribution makes
    every comparison bar the same height, which tells a reader nothing about whether
    the screen works. Weights differ per unit so the charts show a real shape.
    """
    alea = random.Random(_GRAINE)
    commissions = [str(r["id"]) for r in db.fetch_all("SELECT id FROM commission ORDER BY nom", ())]
    tribus = [str(r["id"]) for r in db.fetch_all("SELECT id FROM tribu ORDER BY nom", ())]
    intendances = [str(r["id"]) for r in db.fetch_all("SELECT id FROM intendance ORDER BY nom", ())]
    if not (commissions and tribus and intendances):
        return {"membres": 0}

    membres = db.fetch_all(
        f"SELECT m.id, m.commission_id, m.tribu_id, m.intendance_id FROM membre m "
        f"WHERE {_DEMO} AND m.statut = 'actif'",
        (),
    )
    # Weighted so a few units carry more members than the rest, as a real base does.
    poids_c = [max(1, int(6 * (0.5 + alea.random() ** 2))) for _ in commissions]
    poids_t = [max(1, int(6 * (0.5 + alea.random() ** 2))) for _ in tribus]
    poids_i = [max(1, int(6 * (0.5 + alea.random() ** 2))) for _ in intendances]

    touches = 0
    for m in membres:
        maj: dict[str, str] = {}
        if not m.get("commission_id"):
            maj["commission_id"] = alea.choices(commissions, weights=poids_c, k=1)[0]
        if not m.get("tribu_id"):
            maj["tribu_id"] = alea.choices(tribus, weights=poids_t, k=1)[0]
        if not m.get("intendance_id"):
            maj["intendance_id"] = alea.choices(intendances, weights=poids_i, k=1)[0]
        if not maj:
            continue
        touches += 1
        if simulation:
            continue
        colonnes = ", ".join(f"{c} = %s" for c in maj)
        db.execute(
            f"UPDATE membre m SET {colonnes} WHERE m.id = %s AND {_DEMO}",
            (*maj.values(), str(m["id"])),
        )
    return {"membres": touches}


def _pays_des_membres(simulation: bool) -> int:
    """Fill the country of demonstration members that have none.

    The direction offers a breakdown by country and by continent. With the column
    empty, both collapse into a single "Non renseigné" bar, which reads as a broken
    screen rather than as missing data.
    """
    alea = random.Random(_GRAINE + 1)
    pays = [("Côte d'Ivoire", 70), ("France", 12), ("Canada", 8), ("Belgique", 5), ("États-Unis", 5)]
    noms = [p for p, _ in pays]
    poids = [w for _, w in pays]
    lignes = db.fetch_all(
        f"SELECT m.id FROM membre m WHERE {_DEMO} AND (m.pays IS NULL OR btrim(m.pays) = '')",
        (),
    )
    if not simulation:
        for r in lignes:
            db.execute(
                f"UPDATE membre m SET pays = %s WHERE m.id = %s AND {_DEMO}",
                (alea.choices(noms, weights=poids, k=1)[0], str(r["id"])),
            )
    return len(lignes)


def _participations(simulation: bool) -> dict[str, int]:
    """Record attendance for demonstration members on activities already held.

    Only past and in-progress activities: attendance recorded for something that has
    not happened is not a demonstration, it is a false fact sitting in the base.

    Each member gets an attendance profile, faithful, irregular or distant, and keeps
    it across activities. That is what makes the assiduity cohorts and the trend lines
    say something: a uniform random draw produces a flat, meaningless distribution.
    """
    alea = random.Random(_GRAINE + 2)
    evenements = db.fetch_all(
        "SELECT id, debut FROM evenement WHERE debut IS NOT NULL AND debut <= now() ORDER BY debut",
        (),
    )
    membres = [
        str(r["id"])
        for r in db.fetch_all(
            f"SELECT m.id FROM membre m WHERE {_DEMO} AND m.statut = 'actif' "
            f"AND m.statut_inscription = 'approuve' ORDER BY m.id",
            (),
        )
    ]
    if not (evenements and membres):
        return {"evenements": 0, "participations": 0}

    # Attendance profiles: how often this member shows up at all, and how they follow.
    profils = {}
    for mid in membres:
        tirage = alea.random()
        if tirage < 0.35:
            profils[mid] = {"presence": 0.85, "en_ligne": 0.25}       # assidu
        elif tirage < 0.75:
            profils[mid] = {"presence": 0.55, "en_ligne": 0.45}       # irrégulier
        else:
            profils[mid] = {"presence": 0.25, "en_ligne": 0.60}       # distant

    ecrits = 0
    for ev in evenements:
        eid = str(ev["id"])
        # Not every member is recorded on every activity: a base where everyone has a
        # row for everything has no non-respondents, and the response rate is then
        # always 100 %, which is exactly the figure the direction must be able to doubt.
        vus = [m for m in membres if alea.random() < 0.8]
        for mid in vus:
            p = profils[mid]
            r = alea.random()
            if r < p["presence"]:
                statut = "present"
            elif r < p["presence"] + 0.12:
                statut = "partiel"
            else:
                statut = "absent"
            modalite = "en_ligne" if (statut != "absent" and alea.random() < p["en_ligne"]) else "presentiel"
            ecrits += 1
            if simulation:
                continue
            # ON CONFLICT DO NOTHING on the (activity, member) unique key: this is what
            # makes a second run a no-op, and what guarantees the script can never
            # create the double counting the direction's figures depend on avoiding.
            db.execute(
                "INSERT INTO participation (evenement_id, membre_id, statut, source, valide, modalite) "
                "VALUES (%s, %s, %s, 'declaration', true, %s) "
                "ON CONFLICT (evenement_id, membre_id) DO NOTHING",
                (eid, mid, statut, modalite if statut != "absent" else None),
            )
    return {"evenements": len(evenements), "participations": ecrits}


def main() -> None:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--appliquer", action="store_true", help="écrire réellement")
    analyseur.add_argument("--simulation", action="store_true", help="ne rien écrire (défaut)")
    args = analyseur.parse_args()
    simulation = not args.appliquer

    print("Mode :", "SIMULATION, rien n'est écrit" if simulation else "APPLICATION")
    print()

    nommes = _nommer_demonstration(simulation)
    print(f"  profils renommés en DÉMO                  : {nommes}")
    n = _hierarchie_intendances(simulation)
    print(f"  intendances rattachées à une coordination : {n}")
    r = _rattacher_membres(simulation)
    print(f"  membres de démonstration rattachés        : {r['membres']}")
    p = _pays_des_membres(simulation)
    print(f"  pays renseignés                           : {p}")
    part = _participations(simulation)
    print(f"  activités passées couvertes               : {part['evenements']}")
    print(f"  participations écrites (hors doublons)    : {part['participations']}")

    if simulation:
        print()
        print("Rien n'a été écrit. Relancez avec --appliquer pour enregistrer.")


if __name__ == "__main__":
    main()
