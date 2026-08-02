"""Go and fetch what became of the messages, instead of waiting to be told.

The provider can call back on every delivery, bounce and rejection, and the platform
has an endpoint for exactly that. It has never been configured, so the ledger stayed
empty and nobody could answer the one question that mattered when a
super-administrator could not sign in for two days: was the code even sent, and what
happened to it.

Configuring that callback needs a shared secret placed here and an address pasted
into the provider's console. Both are manual, both were pending, and until they
happen the platform is blind. So it fetches instead of waiting: the provider's API
already answers "what happened to my messages", and the key to call it is already
configured, since it is the same one used to send.

Pull rather than push is also better in one respect that matters here. A callback
that fails is lost, and nothing notices. A poll that fails is simply retried on the
next run, and a gap in the ledger repairs itself.

It stays a poll of the recent window, not a full history: the mailbox diagnostic only
reads the last two days, and pulling more on every run would cost time for answers
nobody asks.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import channels, db

logger = logging.getLogger(__name__)

_API = "https://api.brevo.com/v3/smtp/statistics/events"
_DELAI_S = 20
#: How far back each run looks. Wider than the diagnostic window on purpose, so a run
#: that fails or is skipped is made up for by the next one rather than leaving a hole.
_JOURS = 3
#: Per page, and how many pages at most. The cap is what stops a bad day on the
#: provider's side from turning a scheduled job into an unbounded one.
#:
#: Hitting it is safe here, and that is worth stating because a cap usually is not.
#: The provider returns the newest events first, so truncation drops the oldest of
#: the window; the mailbox diagnostic reads the last two days, and this window is
#: three. What gets dropped is therefore what nobody reads. The run says so anyway,
#: because a cap that stays quiet reads as "everything was collected".
_PAR_PAGE = 100
_PAGES_MAX = 20

#: Provider vocabulary, normalised to the ledger statuses. Kept in step with
#: email_registre._STATUTS_FOURNISSEUR.
_STATUTS = {
    "request": "envoye", "requests": "envoye", "delivered": "delivre",
    "opened": "ouvert", "uniqueOpened": "ouvert", "click": "ouvert",
    "softBounces": "rebondi", "hardBounces": "rebondi", "blocked": "rejete",
    "spam": "rejete", "invalid": "rejete", "deferred": "en_cours", "error": "echoue",
}


def _cle_api() -> str:
    """The sending key, which is also the key that answers about deliveries."""
    return (channels.integration_value("email_api_key") or "").strip()


def _contexte_tls() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - the platform default is still a verified context
        return ssl.create_default_context()


def _page(cle: str, rang: int, contexte: ssl.SSLContext) -> list[dict[str, Any]]:
    requete = urllib.parse.urlencode({"limit": _PAR_PAGE, "offset": rang * _PAR_PAGE, "days": _JOURS})
    demande = urllib.request.Request(
        f"{_API}?{requete}", headers={"api-key": cle, "accept": "application/json"},
    )
    with urllib.request.urlopen(demande, timeout=_DELAI_S, context=contexte) as reponse:
        return json.load(reponse).get("events") or []


def _enregistrer(evenements: list[dict[str, Any]], role: str | None) -> int:
    """Write the events down, skipping the ones already there. Returns how many landed.

    Identity of an event is its recipient, its kind and its instant: the provider
    reports the same event again on every run inside the window, and counting it twice
    would turn one bounce into a pattern.
    """
    if not evenements:
        return 0
    # One lookup for every address at once, rather than one per event: this runs
    # against a remote database, where a round trip per row is the whole cost.
    outbox = {
        str(r["adresse"]): str(r["id"])
        for r in db.fetch_all(
            "SELECT DISTINCT ON (lower(destinataire)) lower(destinataire) AS adresse, id "
            "FROM email_outbox ORDER BY lower(destinataire), cree_le DESC", (), role=role,
        )
    }

    lignes = []
    for e in evenements:
        destinataire = str(e.get("email") or "").strip()
        if not destinataire:
            continue
        brut = str(e.get("event") or "")
        motif = e.get("reason")
        lignes.append((
            outbox.get(destinataire.lower()), destinataire, brut,
            _STATUTS.get(brut, "en_cours"), str(motif)[:500] if motif else None,
            json.dumps(e), e.get("date"),
        ))

    ecrits = 0
    for depart in range(0, len(lignes), 100):
        lot = lignes[depart:depart + 100]
        valeurs = ", ".join(
            ["(%s::uuid, %s, %s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, now()))"] * len(lot)
        )
        params: list[object] = []
        for ligne in lot:
            params.extend(ligne)
        db.execute(
            "INSERT INTO email_delivery_event (email_outbox_id, destinataire, "
            "evenement_fournisseur, statut_normalise, motif, charge_brute, survenu_le) "
            f"SELECT * FROM (VALUES {valeurs}) AS v(a, b, c, d, e, f, g) "
            "WHERE NOT EXISTS (SELECT 1 FROM email_delivery_event x "
            "  WHERE lower(x.destinataire) = lower(v.b) AND x.evenement_fournisseur = v.c "
            "  AND x.survenu_le = v.g)",
            tuple(params), role=role,
        )
        ecrits += len(lot)
    return ecrits


def moissonner(role: str | None = None) -> dict[str, Any]:
    """Pull the recent delivery events and record them. Never raises.

    Called from the daily job, where an exception would cost every other task that
    runs after it. A provider outage is reported in the result and retried tomorrow.
    """
    cle = _cle_api()
    if not cle:
        return {"actif": False, "motif": "aucune clé d'API d'envoi configurée"}

    contexte = _contexte_tls()
    evenements: list[dict[str, Any]] = []
    try:
        for rang in range(_PAGES_MAX):
            lot = _page(cle, rang, contexte)
            if not lot:
                break
            evenements.extend(lot)
    except urllib.error.HTTPError as erreur:
        logger.warning("moisson des livraisons: le fournisseur a répondu %s", erreur.code)
        return {"actif": True, "erreur": f"HTTP {erreur.code}", "recuperes": len(evenements)}
    except Exception as erreur:  # noqa: BLE001 - a scheduled job never dies on a channel
        logger.warning("moisson des livraisons indisponible (%s)", type(erreur).__name__)
        return {"actif": True, "erreur": type(erreur).__name__, "recuperes": len(evenements)}

    try:
        ecrits = _enregistrer(evenements, role)
    except Exception as erreur:  # noqa: BLE001
        logger.warning("moisson des livraisons: écriture impossible (%s)", type(erreur).__name__)
        return {"actif": True, "recuperes": len(evenements), "erreur": "écriture impossible"}

    tronque = len(evenements) >= _PAR_PAGE * _PAGES_MAX
    if tronque:
        # Said out loud rather than passed over: a silent cap reads as "everything was
        # collected" when it was not, which is the exact blindness this module ends.
        logger.info("moisson des livraisons : plafond de %s événements atteint", len(evenements))
    return {
        "actif": True,
        "fenetre_jours": _JOURS,
        "recuperes": len(evenements),
        "examines": ecrits,
        "tronque": tronque,
    }
