"""The lists the direction's filter bar offers, and the period-over-period reading.

Two things that sound unrelated and are not: both exist so a filter means something.
A picker offering units the base does not contain produces an empty screen that reads
as "no attendance here", and a rate with nothing to compare it against cannot say
whether the organisation is improving.

The reference lists are served from one endpoint under ``statistiques.consulter``
rather than borrowed from the back office. The filter bar used to call four
administration endpoints, one of which (the country list) requires ``membres.gerer``
and answered 403 to every direction account: the picker was permanently empty and the
console carried an error on every page load. A steering screen should not depend on
permissions granted for managing members.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import db
from . import direction_analyse as analyse
from . import direction_pilotage as pilotage


def referentiels(role: str | None) -> dict[str, Any]:
    """Every list the filter bar needs, in one round trip.

    Only units that actually carry members are returned. Offering a commission nobody
    belongs to hands the reader a filter whose only possible result is an empty
    screen, and an empty screen is indistinguishable from a broken one.
    """
    def lister(sql: str) -> list[dict[str, str]]:
        return [
            {"id": str(r["id"]), "nom": str(r["nom"] or "Sans nom"), "effectif": int(r["effectif"] or 0)}
            for r in db.fetch_all(sql, (), role=role)
        ]

    coordinations = lister(
        "SELECT co.id, co.nom, count(m.id) AS effectif FROM coordination co "
        "LEFT JOIN intendance i ON i.coordination_id = co.id "
        "LEFT JOIN membre m ON (m.coordination_id = co.id OR m.intendance_id = i.id) "
        "GROUP BY co.id, co.nom ORDER BY co.nom"
    )
    intendances = lister(
        "SELECT i.id, i.nom, count(m.id) AS effectif FROM intendance i "
        "LEFT JOIN membre m ON m.intendance_id = i.id GROUP BY i.id, i.nom ORDER BY i.nom"
    )
    commissions = [
        {**c, "type": t}
        for c, t in (
            (
                {"id": str(r["id"]), "nom": str(r["nom"] or "Sans nom"), "effectif": int(r["effectif"] or 0)},
                str(r["type_organisation"] or "commission"),
            )
            for r in db.fetch_all(
                "SELECT c.id, c.nom, c.type_organisation, count(m.id) AS effectif FROM commission c "
                "LEFT JOIN membre m ON m.commission_id = c.id "
                "GROUP BY c.id, c.nom, c.type_organisation ORDER BY c.nom",
                (), role=role,
            )
        )
    ]
    tribus = lister(
        "SELECT t.id, t.nom, count(m.id) AS effectif FROM tribu t "
        "LEFT JOIN membre m ON m.tribu_id = t.id GROUP BY t.id, t.nom ORDER BY t.nom"
    )

    # Countries and volets come from the member and activity records rather than a
    # reference table: what matters to a filter is what the data contains.
    pays = [
        {"id": str(r["pays"]), "nom": str(r["pays"]), "effectif": int(r["effectif"] or 0)}
        for r in db.fetch_all(
            "SELECT btrim(pays) AS pays, count(*) AS effectif FROM membre "
            "WHERE pays IS NOT NULL AND btrim(pays) <> '' GROUP BY 1 ORDER BY 2 DESC, 1",
            (), role=role,
        )
    ]
    volets = [
        {"id": str(r["volet"]), "nom": f"Volet {r['volet']}", "effectif": int(r["effectif"] or 0)}
        for r in db.fetch_all(
            "SELECT volet, count(*) AS effectif FROM evenement "
            "WHERE volet IS NOT NULL AND btrim(volet) <> '' GROUP BY 1 ORDER BY 1",
            (), role=role,
        )
    ]
    genres = [
        {"id": str(r["genre"]), "nom": {"M": "Masculin", "F": "Féminin"}.get(str(r["genre"]), str(r["genre"])),
         "effectif": int(r["effectif"] or 0)}
        for r in db.fetch_all(
            "SELECT genre, count(*) AS effectif FROM membre "
            "WHERE genre IS NOT NULL AND btrim(genre) <> '' GROUP BY 1 ORDER BY 1",
            (), role=role,
        )
    ]
    types_membre = [
        {"id": str(r["type_membre"]), "nom": str(r["type_membre"]), "effectif": int(r["effectif"] or 0)}
        for r in db.fetch_all(
            "SELECT type_membre, count(*) AS effectif FROM membre "
            "WHERE type_membre IS NOT NULL AND btrim(type_membre) <> '' GROUP BY 1 ORDER BY 2 DESC",
            (), role=role,
        )
    ]

    # How much of the base is demonstration data. Served rather than assumed, so the
    # screen can warn with a real figure instead of a hard-coded sentence that would
    # go stale the day the profiles are purged.
    d = db.fetch_one(
        "SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE {analyse._EST_DEMO}) AS demonstration "
        "FROM membre m WHERE m.statut = 'actif'",
        {}, role=role,
    ) or {}
    total = int(d.get("total") or 0)
    demo = int(d.get("demonstration") or 0)

    return {
        "coordinations": coordinations,
        "intendances": intendances,
        "commissions": commissions,
        "tribus": tribus,
        "pays": pays,
        "volets": volets,
        "genres": genres,
        "types_membre": types_membre,
        "valeurs_enumerees": analyse.valeurs_enumerees(),
        "demonstration": {
            "membres_total": total,
            "membres_demonstration": demo,
            "part": round(100.0 * demo / total, 1) if total else 0.0,
        },
    }


def _fenetre(filtres: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """The period the filters describe, or None when they describe no period.

    A comparison needs a window of known length. Without one there is nothing to shift
    backwards, and inventing a default would compare the reader to a period they never
    asked about.
    """
    depuis = filtres.get("depuis")
    if not depuis:
        return None
    try:
        debut = datetime.fromisoformat(str(depuis))
    except ValueError:
        return None
    fin_brute = filtres.get("jusqu_a")
    if fin_brute:
        try:
            fin = datetime.fromisoformat(str(fin_brute))
        except ValueError:
            return None
    else:
        # An open-ended window runs to today, which is what the reader sees on screen.
        fin = datetime.now(debut.tzinfo)
    return (debut, fin) if fin > debut else None


def _delta(courant: float, precedent: float) -> dict[str, Any]:
    """The movement between two figures, with its direction named.

    A percentage change on a rate is misleading, so a rate moves in points and a count
    moves in per cent. Conflating the two is how "up 12 %" gets read as twelve points.
    """
    return {
        "courant": courant,
        "precedent": precedent,
        "ecart": round(courant - precedent, 1),
        "evolution_pct": round(100.0 * (courant - precedent) / precedent, 1) if precedent else None,
        "sens": "hausse" if courant > precedent else "baisse" if courant < precedent else "stable",
    }


def comparaison(filtres: dict[str, Any], role: str | None) -> dict[str, Any]:
    """This period against the one immediately before it, of the same length.

    Same length, not "last month": comparing a fortnight with a quarter produces a
    collapse that is entirely an artefact of the window. The previous window ends the
    instant this one starts, so no activity is counted in both.
    """
    fenetre = _fenetre(filtres)
    courant = pilotage.synthese(filtres, role)
    if fenetre is None:
        return {
            "disponible": False,
            "raison": (
                "Choisissez une période pour comparer : sans fenêtre de temps, "
                "il n'y a pas de période précédente de même durée à opposer."
            ),
            "courant": courant,
        }

    debut, fin = fenetre
    duree = fin - debut
    # One microsecond back, so the previous window ends before this one begins and no
    # activity falls in both.
    precedent_fin = debut - timedelta(microseconds=1)
    precedent_debut = precedent_fin - duree

    filtres_precedents = {
        **filtres,
        "depuis": precedent_debut.isoformat(),
        "jusqu_a": precedent_fin.isoformat(),
    }
    precedent = pilotage.synthese(filtres_precedents, role)

    mesures = {
        "taux_presence": _delta(courant["taux_presence"], precedent["taux_presence"]),
        "taux_participation": _delta(courant["taux_participation"], precedent["taux_participation"]),
        "presents": _delta(courant["presents"], precedent["presents"]),
        "observations": _delta(courant["observations"], precedent["observations"]),
        "activites": _delta(courant["activites"], precedent["activites"]),
        "membres_vus": _delta(courant["membres_vus"], precedent["membres_vus"]),
        "moyenne_par_activite": _delta(courant["moyenne_par_activite"], precedent["moyenne_par_activite"]),
    }

    # A comparison against a period that held nothing is not a comparison. Said out
    # loud rather than shown as a spectacular rise from zero.
    exploitable = precedent["observations"] > 0
    return {
        "disponible": True,
        "exploitable": exploitable,
        "avertissement": None if exploitable else (
            "La période précédente ne contient aucune observation : "
            "l'écart affiché ne mesure rien."
        ),
        "periode_courante": {"depuis": debut.isoformat(), "jusqu_a": fin.isoformat()},
        "periode_precedente": {"depuis": precedent_debut.isoformat(), "jusqu_a": precedent_fin.isoformat()},
        "courant": courant,
        "precedent": precedent,
        "mesures": mesures,
    }
