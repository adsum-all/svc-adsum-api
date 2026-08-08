"""Support threads: what must hold whatever the provider sends.

The parsing rules run without a database. The attachment rules run against the real
one, create their own thread, and delete it: a test that leaves rows behind pollutes
the very queue an agent is meant to trust.
"""
from __future__ import annotations

import os

import pytest

from app.support_entrant import _REFERENCE, _corps_utile, _premier

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("ADSUM_DATABASE_URL"),
    reason="real database not available",
)


# --- Parsing, no database ----------------------------------------------------

def test_l_historique_cite_est_retire_de_la_reponse() -> None:
    """Each exchange would otherwise be longer than the last, burying the answer."""
    assert _corps_utile("Bonjour.\n\nLe 08/08/2026 quelqu'un a écrit :\n> ancien") == "Bonjour."
    # The accent is often dropped by a client or a sender; missing the marker for that
    # reason would keep the entire history.
    assert _corps_utile("Bonjour.\n\nLe 08/08/2026 quelqu'un a ecrit :\n> ancien") == "Bonjour."
    assert _corps_utile("Bien reçu.\n\nEnvoyé de mon iPhone") == "Bien reçu."
    assert _corps_utile("Ok.\n--\nJean Dupont") == "Ok."
    assert _corps_utile("Ok.\n________________\nDe : quelqu'un") == "Ok."


def test_une_reponse_entierement_citee_n_est_jamais_vide() -> None:
    """The body column refuses null, and a short reply still carries intent."""
    assert _corps_utile("> tout est cité").strip()
    assert _corps_utile("").strip()


def test_les_noms_de_champs_des_differents_analyseurs_sont_acceptes() -> None:
    """One vendor's shape would make a change of provider a code change."""
    assert _premier({"text": "a"}, "text", "TextBody") == "a"
    assert _premier({"TextBody": "b"}, "text", "TextBody") == "b"
    assert _premier({"from": {"Email": "x@y.fr"}}, "from") == "x@y.fr"
    assert _premier({"to": [{"address": "z@y.fr"}]}, "to") == "z@y.fr"
    assert _premier({}, "absent") == ""


def test_la_reference_est_reconnue_dans_un_objet_de_reponse() -> None:
    """The reference is the only signal that survives a client rewriting the subject."""
    trouve = _REFERENCE.search("Re: [SUP-2026-0042] Je ne reçois plus rien")
    assert trouve is not None
    assert trouve.group(0) == "SUP-2026-0042"
    assert _REFERENCE.search("Re: aucune référence ici") is None


# --- Attachment, real database ----------------------------------------------

@pytestmark_db
def test_un_message_entrant_ouvre_puis_retrouve_son_fil() -> None:
    """Reference, then sender, then a new thread: never a dropped message."""
    from app import db
    from app.support_entrant import traiter_message_entrant

    adresse = "test.support.entrant@exemple.invalid"
    db.execute("DELETE FROM support_fil WHERE demandeur_email = %s", (adresse,))
    try:
        ouverture = traiter_message_entrant(
            {"from": adresse, "subject": "Une première demande", "text": "Le contenu.",
             "message_id": "<t1@exemple.invalid>"}
        )
        assert ouverture["duplique"] is False
        fil = ouverture["fil"]

        # Same identifier again: the unique index absorbs the retry, and the answer
        # still names the thread so the provider is not told the message went nowhere.
        rejeu = traiter_message_entrant(
            {"from": adresse, "subject": "autre objet", "text": "doublon", "message_id": "<t1@exemple.invalid>"}
        )
        assert rejeu["duplique"] is True
        assert rejeu["reference"] == ouverture["reference"]

        # Reply carrying the reference lands on the same thread.
        suite = traiter_message_entrant(
            {
                "from": adresse,
                "subject": f"Re: [{ouverture['reference']}] Une première demande",
                "text": "La suite.",
                "message_id": "<t2@exemple.invalid>",
            }
        )
        assert suite["fil"] == fil

        # Reply with no reference at all still finds the open thread by sender.
        sans_reference = traiter_message_entrant(
            {"from": adresse, "subject": "objet retapé", "text": "Encore.", "message_id": "<t3@exemple.invalid>"}
        )
        assert sans_reference["fil"] == fil

        compte = db.fetch_one("SELECT count(*) AS n FROM support_message WHERE fil_id = %s", (fil,))
        assert compte is not None and int(compte["n"]) == 3
    finally:
        db.execute("DELETE FROM support_fil WHERE demandeur_email = %s", (adresse,))


@pytestmark_db
def test_un_fil_clos_qui_recoit_une_reponse_est_rouvert() -> None:
    """A closed thread that receives an answer is not closed: the person is still talking.

    Appending silently would leave the message in a conversation nobody reopens.
    """
    from app import db
    from app.support_entrant import traiter_message_entrant

    adresse = "test.support.relance@exemple.invalid"
    db.execute("DELETE FROM support_fil WHERE demandeur_email = %s", (adresse,))
    try:
        ouverture = traiter_message_entrant(
            {"from": adresse, "subject": "Demande à clore", "text": "Contenu.", "message_id": "<r1@exemple.invalid>"}
        )
        fil = ouverture["fil"]
        db.execute("UPDATE support_fil SET statut = 'clos', ferme_le = now() WHERE id = %s", (fil,))

        traiter_message_entrant(
            {
                "from": adresse,
                "subject": f"Re: [{ouverture['reference']}] rechute",
                "text": "Le problème revient.",
                "message_id": "<r2@exemple.invalid>",
            }
        )
        etat = db.fetch_one("SELECT statut, ferme_le FROM support_fil WHERE id = %s", (fil,))
        assert etat is not None
        assert etat["statut"] == "en_cours"
        # The table refuses an open thread carrying a closing date, so this also proves
        # the constraint was honoured rather than bypassed.
        assert etat["ferme_le"] is None
    finally:
        db.execute("DELETE FROM support_fil WHERE demandeur_email = %s", (adresse,))
