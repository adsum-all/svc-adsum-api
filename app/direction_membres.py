"""Nominative attendance follow-up for the direction, deliberately minimised.

The direction asked for a member list. The back office already has one, and copying
it here would widen the personal-data surface for no steering gain: a second screen
showing telephone numbers, addresses, identity documents and photographs, under a
different permission, ageing separately.

What steering actually needs from a name is narrower, and it is a real need the
aggregates cannot serve: knowing WHO has stopped coming, so somebody can reach out.
A rate of sixty per cent tells a leader something is wrong; it never tells them whom
to call.

So this returns the smallest set of fields that answers that question, and nothing
else. The whitelist in :data:`_CHAMPS` is the entire contract, and it is short on
purpose: name, unit attachment, attendance figures, last attendance. No contact
details, no address, no date of birth, no document, no photograph. Anyone auditing
what the direction can see about a person reads one list, in one file.

Access is gated on ``membres.consulter``, which the direction already holds for the
back office. This endpoint returns strictly less than that permission allows, never
more.
"""
from __future__ import annotations

from typing import Any

from . import db
from .direction_analyse import _CONSO, _JOINTURES, _where

#: Everything the direction may learn about a named person here. The list is the
#: contract: a field absent from it is absent from the response, and adding one is a
#: deliberate act somebody has to review, not a side effect of widening a SELECT.
_CHAMPS = (
    "nom_affiche", "coordination", "intendance", "commission", "tribu",
    "observees", "suivies", "taux", "derniere_presence", "cohorte",
)

_INCONNU = "Non renseigné"

_TRIS = {
    "taux": "taux ASC, nom_affiche ASC",
    "taux_desc": "taux DESC, nom_affiche ASC",
    "nom": "nom_affiche ASC",
    "recent": "derniere_presence DESC NULLS LAST, nom_affiche ASC",
    "ancien": "derniere_presence ASC NULLS FIRST, nom_affiche ASC",
}


def _cohorte(taux: float, observees: int) -> str:
    """Name the band, or say the data is too thin to name one.

    A single absence must not label somebody as having dropped out. The direction
    acts on these labels, so a label the data cannot support is worse than none.
    """
    if observees < 2:
        return "donnees_insuffisantes"
    if taux >= 80:
        return "assidu"
    if taux >= 50:
        return "regulier"
    if taux >= 20:
        return "irregulier"
    return "decroche"


def suivi_assiduite(
    filtres: dict[str, Any], role: str | None,
    tri: str = "taux", limite: int = 100, decalage: int = 0,
) -> dict[str, Any]:
    """Members and how often they attend, under the current filters.

    Sorted by lowest attendance first by default: the list exists so somebody can act
    on the people at the top of it, and burying them under the assiduous members
    would defeat the only reason it is nominative.
    """
    ordre = _TRIS.get(tri, _TRIS["taux"])
    limite = max(1, min(int(limite), 500))
    decalage = max(0, int(decalage))
    where, params = _where(filtres)

    lignes = db.fetch_all(
        f"WITH {_CONSO}, "
        "agrege AS ("
        "  SELECT cc.membre_id, "
        # nom_affiche is a cached column and is filled for two members out of
        # seventy-six, so relying on it alone leaves the list almost entirely
        # anonymous. Composed here from the civil identity, family name first, which
        # is how the platform writes a person's name everywhere else.
        "    max(coalesce("
        "      nullif(btrim(m.nom_affiche), ''), "
        "      nullif(btrim(concat_ws(' ', m.nom, m.prenoms)), ''), "
        "      'Membre sans nom')) AS nom_affiche, "
        f"    max(coalesce(co.nom, cod.nom, '{_INCONNU}')) AS coordination, "
        f"    max(coalesce(i.nom, '{_INCONNU}')) AS intendance, "
        f"    max(coalesce(c.nom, '{_INCONNU}')) AS commission, "
        f"    max(coalesce(t.nom, '{_INCONNU}')) AS tribu, "
        "    count(*) AS observees, "
        "    count(*) FILTER (WHERE cc.present OR cc.partiel) AS suivies, "
        "    max(e.debut) FILTER (WHERE cc.present OR cc.partiel) AS derniere_presence "
        f"  FROM conso cc {_JOINTURES} WHERE {where} "
        "  GROUP BY cc.membre_id"
        ") "
        "SELECT *, round(100.0 * suivies / nullif(observees, 0), 1) AS taux, "
        "count(*) OVER () AS total_lignes "
        f"FROM agrege ORDER BY {ordre} LIMIT %(limite)s OFFSET %(decalage)s",
        {**params, "limite": limite, "decalage": decalage}, role=role,
    )

    membres = []
    for r in lignes:
        observees = int(r["observees"] or 0)
        taux = float(r["taux"] or 0.0)
        membres.append({
            "nom_affiche": r["nom_affiche"] or _INCONNU,
            "coordination": r["coordination"],
            "intendance": r["intendance"],
            "commission": r["commission"],
            "tribu": r["tribu"],
            "observees": observees,
            "suivies": int(r["suivies"] or 0),
            "taux": taux,
            "derniere_presence": (
                r["derniere_presence"].isoformat() if r["derniere_presence"] else None
            ),
            "cohorte": _cohorte(taux, observees),
        })

    # Asserted rather than assumed: the whitelist above is only a guarantee if the
    # rows actually match it. A column added to the query later fails here instead of
    # quietly reaching the direction's screen.
    for m in membres:
        assert set(m) == set(_CHAMPS), f"champ hors périmètre : {set(m) ^ set(_CHAMPS)}"

    return {
        "membres": membres,
        "total": int(lignes[0]["total_lignes"]) if lignes else 0,
        "limite": limite,
        "decalage": decalage,
        "champs_exposes": list(_CHAMPS),
    }
