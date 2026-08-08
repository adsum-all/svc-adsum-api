"""Whether a member may be checked in, and what the controller must be told.

The control application has two jobs at once: verify who somebody is, and record that
they were there. Until now it did the second without ever doing the first properly.
The QR signature was checked, but the profile behind it was not: a suspended member,
an archived file, a refused registration all produced the same green screen, and the
presence was written in silence.

This is the decision matrix. It answers with one of three verdicts, and always with a
reason the controller can read out loud at the door.

``autorise``  the presence may be recorded.
``alerte``    it may be recorded, but something is wrong and the controller must see it.
``refuse``    no presence is written, whatever the interface does.

Two rules shape the boundary between them.

Being physically present is a fact, not a permission. Somebody standing at the door
whose invitation e-mail was never opened is still standing at the door. The rules that
govern who receives a message, in :mod:`app.eligibilite`, are deliberately stricter
than these: refusing a check-in for a dormant account would erase an attendance that
genuinely happened.

What is refused is a file the organisation has closed. Suspended, archived, inactive,
or a registration that was refused: in those cases the organisation has already
decided this person does not attend, and the door is not the place to overturn it.

The verdict is computed on the server. The colour on the controller's screen is a
consequence of it, never the decision itself, so a modified client cannot check
anybody in.
"""
from __future__ import annotations

from typing import Any

from . import db

#: Verdicts, ordered from most to least permissive.
AUTORISE = "autorise"
ALERTE = "alerte"
REFUSE = "refuse"

#: Member states the organisation has closed. A check-in is refused outright.
_FICHES_FERMEES = {
    "suspendu": "fiche membre suspendue",
    "archive": "fiche membre archivée",
    "inactif": "fiche membre inactive",
    "desactive": "fiche membre désactivée",
}

#: Registration states that block. A refused file is a decision already taken.
_INSCRIPTIONS_BLOQUANTES = {
    "refuse": "dossier d'inscription refusé",
}

#: Registration states that warn without blocking. The person is at the door; the
#: organisation will want to know their file is not finished, but turning them away
#: would lose an attendance that really happened.
_INSCRIPTIONS_ALERTE = {
    "incomplet": "dossier d'inscription incomplet",
    "soumis": "dossier soumis, en attente de revue",
    "en_revue": "dossier en cours de revue",
    "modification_demandee": "modification demandée sur le dossier",
}


def etat_pour_pointage(membre_id: str, role: str | None = None) -> dict[str, Any]:
    """The check-in verdict for one member, with everything the controller must see.

    Never raises: a lookup failure yields a refusal with a readable reason rather than
    an exception the scanner would swallow into a blank screen.
    """
    try:
        row = db.fetch_one(
            "SELECT m.id, m.statut, m.statut_inscription, coalesce(m.verifie, false) AS verifie, "
            "u.id AS compte_id, coalesce(u.actif, true) AS compte_actif "
            "FROM membre m LEFT JOIN utilisateur u ON u.membre_id = m.id WHERE m.id = %s",
            (membre_id,), role=role,
        )
    except Exception:  # noqa: BLE001 - a broken lookup must not read as an approval
        return {
            "verdict": REFUSE, "code": "lecture_impossible",
            "raison": "Profil illisible, pointage refusé",
            "statut": None, "statut_inscription": None, "identite_verifiee": False,
        }

    if not row:
        return {
            "verdict": REFUSE, "code": "membre_inconnu",
            "raison": "Membre inconnu",
            "statut": None, "statut_inscription": None, "identite_verifiee": False,
        }

    statut = str(row.get("statut") or "")
    inscription = str(row.get("statut_inscription") or "")
    verifie = bool(row.get("verifie"))

    base = {
        "statut": statut or None,
        "statut_inscription": inscription or None,
        "identite_verifiee": verifie,
    }

    if statut in _FICHES_FERMEES:
        return {**base, "verdict": REFUSE, "code": f"membre_{statut}",
                "raison": _FICHES_FERMEES[statut].capitalize()}
    if statut != "actif":
        return {**base, "verdict": REFUSE, "code": "membre_statut_inconnu",
                "raison": f"Statut de fiche non reconnu : {statut or 'vide'}"}
    if inscription in _INSCRIPTIONS_BLOQUANTES:
        return {**base, "verdict": REFUSE, "code": f"inscription_{inscription}",
                "raison": _INSCRIPTIONS_BLOQUANTES[inscription].capitalize()}
    if inscription in _INSCRIPTIONS_ALERTE:
        return {**base, "verdict": ALERTE, "code": f"inscription_{inscription}",
                "raison": _INSCRIPTIONS_ALERTE[inscription].capitalize()}
    if inscription != "approuve":
        return {**base, "verdict": ALERTE, "code": "inscription_inconnue",
                "raison": f"Inscription au statut {inscription or 'vide'}"}
    if not verifie:
        # Recorded and shown, not blocked: the identity check is exactly what the
        # controller is doing right now, face to face with the photograph.
        return {**base, "verdict": ALERTE, "code": "identite_non_verifiee",
                "raison": "Identité non encore vérifiée par l'administration"}
    return {**base, "verdict": AUTORISE, "code": "conforme",
            "raison": "Profil validé et actif"}


def refuse(etat: dict[str, Any]) -> bool:
    return etat.get("verdict") == REFUSE
