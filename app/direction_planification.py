"""The automatic communication channel: reports that go out on their own.

A dashboard is read by whoever thinks to open it, and the people who most need the
attendance figures are the least likely to log in on a Monday morning. A subscription
turns that around: the platform sends the report to the people who asked for it, on
the cadence they chose, with the scope they set.

Two properties this rests on.

The scope is stored, not the rows. A subscription created in July must describe August
when August arrives; freezing the table would send July's figures forever under a
fresh date. The filters are replayed against the consolidation at send time, so the
report is always about the period it claims.

Running the job twice sends nothing twice. Each row carries its last dispatch and the
cadence is measured against it, which is what makes an hourly scheduler safe to point
at this. A report that arrives four times because the scheduler retried is a report
people filter out of their inbox.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from . import audit, db
from . import direction_analyse as analyse
from . import direction_pilotage as pilotage
from .direction_rapport import RapportEnvoi, envoyer_maintenant

#: How long must pass before a subscription is due again. Measured from the last
#: dispatch rather than against a calendar boundary, so a job that runs late does not
#: skip a period entirely.
_CADENCES: dict[str, timedelta] = {
    "quotidien": timedelta(hours=20),
    "hebdomadaire": timedelta(days=6, hours=12),
    "mensuel": timedelta(days=28),
}

#: How far back each cadence looks. A weekly report about the whole history is not a
#: weekly report; the window matches what the reader expects to have changed.
_FENETRES: dict[str, timedelta] = {
    "quotidien": timedelta(days=1),
    "hebdomadaire": timedelta(days=7),
    "mensuel": timedelta(days=30),
}


def _ligne(r: dict[str, Any]) -> dict[str, Any]:
    filtres = r.get("filtres")
    if isinstance(filtres, str):
        try:
            filtres = json.loads(filtres)
        except ValueError:
            filtres = {}
    return {
        "id": str(r["id"]),
        "libelle": r["libelle"],
        "destinataires": list(r["destinataires"] or []),
        "frequence": r["frequence"],
        "dimension": r["dimension"],
        "filtres": filtres or {},
        "actif": bool(r["actif"]),
        "dernier_envoi": r["dernier_envoi"].isoformat() if r.get("dernier_envoi") else None,
        "dernier_statut": r.get("dernier_statut"),
    }


def lister(role: str | None) -> list[dict[str, Any]]:
    return [
        _ligne(r)
        for r in db.fetch_all(
            "SELECT id, libelle, destinataires, frequence, dimension, filtres, actif, "
            "dernier_envoi, dernier_statut FROM direction_rapport_planifie "
            "ORDER BY actif DESC, libelle",
            (), role=role,
        )
    ]


def creer(
    libelle: str, destinataires: list[str], frequence: str, dimension: str,
    filtres: dict[str, Any], acteur_id: str | None, role: str | None,
) -> dict[str, Any]:
    """Register a subscription, refusing anything the platform cannot honour.

    The dimension and the filters are validated here rather than at send time. A
    subscription accepted with an unknown axis would sit in the table looking healthy
    and fail silently every night, which is the worst of both.
    """
    if frequence not in _CADENCES:
        raise analyse.DimensionInconnue(
            f"fréquence inconnue : {frequence}. Attendu : {', '.join(sorted(_CADENCES))}."
        )
    if dimension not in analyse.DIMENSIONS:
        raise analyse.DimensionInconnue(dimension)
    # Replayed once now, so a broken scope is refused in front of the person creating
    # it instead of failing every night with nobody watching.
    analyse.repartition(dimension, filtres, role)

    r = db.fetch_one(
        "INSERT INTO direction_rapport_planifie "
        "(libelle, destinataires, frequence, dimension, filtres, cree_par) "
        "VALUES (%(libelle)s, %(destinataires)s, %(frequence)s, %(dimension)s, "
        "%(filtres)s::jsonb, %(acteur)s) "
        "RETURNING id, libelle, destinataires, frequence, dimension, filtres, actif, "
        "dernier_envoi, dernier_statut",
        {
            "libelle": libelle,
            "destinataires": destinataires,
            "frequence": frequence,
            "dimension": dimension,
            "filtres": json.dumps(filtres),
            "acteur": acteur_id,
        },
        role=role,
    )
    audit.log(
        acteur_id, role, "creation_rapport_planifie", "rapport_planifie",
        str(r["id"]) if r else None,
        {"libelle": libelle, "frequence": frequence, "destinataires": len(destinataires)},
    )
    return _ligne(r or {})


def basculer(abonnement_id: str, actif: bool, acteur_id: str | None, role: str | None) -> bool:
    r = db.fetch_one(
        "UPDATE direction_rapport_planifie SET actif = %s, maj_le = now() "
        "WHERE id = %s RETURNING id",
        (actif, abonnement_id), role=role,
    )
    if r:
        audit.log(
            acteur_id, role, "bascule_rapport_planifie", "rapport_planifie",
            abonnement_id, {"actif": actif},
        )
    return bool(r)


def supprimer(abonnement_id: str, acteur_id: str | None, role: str | None) -> bool:
    r = db.fetch_one(
        "DELETE FROM direction_rapport_planifie WHERE id = %s RETURNING id",
        (abonnement_id,), role=role,
    )
    if r:
        audit.log(
            acteur_id, role, "suppression_rapport_planifie", "rapport_planifie",
            abonnement_id, {},
        )
    return bool(r)


def _composer(abonnement: dict[str, Any], role: str | None) -> RapportEnvoi | None:
    """Build the report this subscription describes, for the period now ending.

    Returns None when the window holds nothing. An empty table sent every week teaches
    the reader to ignore the message, and by the time something does happen they no
    longer open it.
    """
    fenetre = _FENETRES.get(str(abonnement["frequence"]), timedelta(days=7))
    depuis = (datetime.now(UTC) - fenetre).isoformat()
    filtres = {**abonnement["filtres"], "depuis": depuis}

    lignes = analyse.repartition(str(abonnement["dimension"]), filtres, role)
    if not lignes:
        return None
    synthese = pilotage.synthese(filtres, role)

    libelle_axe = analyse.DIMENSIONS[str(abonnement["dimension"])]["libelle"]
    contexte = (
        f"Période du {depuis[:10]} à aujourd'hui. "
        f"{synthese['observations']} observations, {synthese['presents']} présences, "
        f"taux de présence {synthese['taux_presence']} %, "
        f"couverture {synthese['taux_couverture']} % des membres du périmètre."
    )
    return RapportEnvoi(
        destinataire=str(abonnement["destinataires"][0]),
        titre=str(abonnement["libelle"]),
        colonnes=[libelle_axe, "Présents", "Partiels", "Absents", "Total", "Taux de suivi"],
        lignes=[
            [
                str(x["label"]), str(x["presents"]), str(x["partiels"]),
                str(x["absents"]), str(x["total"]), f"{x['taux_participation']} %",
            ]
            for x in lignes
        ],
        contexte=contexte,
    )


def _envoyer(ab: dict[str, Any], role: str | None) -> dict[str, Any]:
    """Compose and dispatch one subscription, recording what happened on its row."""
    rapport = _composer(ab, role)
    if rapport is None:
        db.execute(
            "UPDATE direction_rapport_planifie SET dernier_envoi = now(), "
            "dernier_statut = 'aucune donnée sur la période' WHERE id = %s",
            (ab["id"],), role=role,
        )
        return {"id": ab["id"], "vide": True, "envoyes": 0, "echoues": 0,
                "statut": "aucune donnée sur la période"}

    # One send per recipient rather than one message carrying everyone in the To
    # field: a direction report names units and figures, and who else receives it is
    # not something each reader needs to learn.
    envoyes, echoues = 0, 0
    for adresse in ab["destinataires"]:
        ok, _ = envoyer_maintenant(rapport, adresse)
        if ok:
            envoyes += 1
        else:
            echoues += 1

    statut = f"{envoyes} envoyé(s), {echoues} refusé(s)"
    db.execute(
        "UPDATE direction_rapport_planifie SET dernier_envoi = now(), "
        "dernier_statut = %s WHERE id = %s",
        (statut, ab["id"]), role=role,
    )
    audit.log(
        None, role, "envoi_rapport_planifie", "rapport_planifie", ab["id"],
        {"libelle": ab["libelle"], "envoyes": envoyes, "echoues": echoues},
    )
    return {"id": ab["id"], "vide": False, "envoyes": envoyes, "echoues": echoues,
            "statut": statut, "lignes": len(rapport.lignes)}


def executer_un(abonnement_id: str, force: bool, role: str | None) -> dict[str, Any] | None:
    """Run one subscription, optionally ignoring its cadence.

    ``force`` exists so a subscription can be proved before anybody trusts it to a
    schedule. Without it the only way to learn that a recurring report is broken is to
    wait for the night it should have arrived and notice that it did not.
    """
    r = db.fetch_one(
        "SELECT id, libelle, destinataires, frequence, dimension, filtres, actif, "
        "dernier_envoi, dernier_statut FROM direction_rapport_planifie WHERE id = %s",
        (abonnement_id,), role=role,
    )
    if not r:
        return None
    ab = _ligne(r)
    if not force:
        cadence = _CADENCES.get(ab["frequence"], timedelta(days=7))
        if ab["dernier_envoi"]:
            precedent = datetime.fromisoformat(ab["dernier_envoi"])
            if datetime.now(UTC) - precedent < cadence:
                return {"id": ab["id"], "ignore": True, "statut": "pas encore dû"}
    return _envoyer(ab, role)


def executer_les_dus(role: str | None = None) -> dict[str, Any]:
    """Send every subscription whose cadence has elapsed. Never raises.

    Called from the daily job, which must survive one failing subscription: a bad
    address on one report is no reason for the others not to go out, nor for the rest
    of the nightly work to stop.
    """
    resultat = {"examines": 0, "envoyes": 0, "vides": 0, "echecs": 0}
    try:
        abonnements = [_ligne(r) for r in db.fetch_all(
            "SELECT id, libelle, destinataires, frequence, dimension, filtres, actif, "
            "dernier_envoi, dernier_statut FROM direction_rapport_planifie WHERE actif",
            (), role=role,
        )]
    except Exception as exc:  # noqa: BLE001 - the nightly job must not die here
        return {**resultat, "erreur": str(exc)[:200]}

    maintenant = datetime.now(UTC)
    for ab in abonnements:
        resultat["examines"] += 1
        try:
            cadence = _CADENCES.get(ab["frequence"], timedelta(days=7))
            if ab["dernier_envoi"]:
                precedent = datetime.fromisoformat(ab["dernier_envoi"])
                if maintenant - precedent < cadence:
                    continue
            issue = _envoyer(ab, role)
            resultat["vides"] += 1 if issue["vide"] else 0
            resultat["envoyes"] += issue["envoyes"]
            resultat["echecs"] += issue["echoues"]
        except Exception as exc:  # noqa: BLE001 - one bad subscription, not all of them
            resultat["echecs"] += 1
            try:
                db.execute(
                    "UPDATE direction_rapport_planifie SET dernier_statut = %s WHERE id = %s",
                    (f"erreur : {str(exc)[:120]}", ab["id"]), role=role,
                )
            except Exception:  # noqa: BLE001, S110 - already in the failure path
                pass
    return resultat
