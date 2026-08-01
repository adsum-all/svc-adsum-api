"""Record what the platform tried to send, and what became of it.

A send used to be judged on the provider returning a 2xx, and that verdict was
written as ``email_envoye: true``. It only says the request was accepted. One
registrant carries that very line in the journal and has no trace at all in the
provider's log; another has every message soft-bounced by their mailbox. Both looked
like successes from inside the application, which is why nobody could answer the
simple question a member was asking: did you actually send me anything?

This module answers it. Every send is written down before it leaves, updated with
its outcome, and enriched later by whatever the provider reports back.

Nothing here may ever prevent a message from going out: an observability layer that
can block deliveries is worse than no observability at all. Every failure is
swallowed on purpose, and the send proceeds.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import db

# Provider vocabulary, normalised to the outbox statuses. Anything unknown is kept
# verbatim in the event row rather than guessed at.
_STATUTS_FOURNISSEUR = {
    "request": "envoye",
    "requests": "envoye",
    "delivered": "delivre",
    "opened": "ouvert",
    "uniqueOpened": "ouvert",
    "click": "ouvert",
    "softBounces": "rebondi",
    "hardBounces": "rebondi",
    "blocked": "rejete",
    "spam": "rejete",
    "invalid": "rejete",
    "deferred": "en_cours",
    "error": "echoue",
}


def cle_idempotence(destinataire: str, template_code: str, reference: str = "") -> str:
    """A stable key for one message, one recipient, one reason.

    Asking twice for the same thing must produce one e-mail. Hashed rather than
    concatenated so an address never sits in an index in clear text.
    """
    brut = f"{destinataire.strip().lower()}|{template_code}|{reference}"
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


def enregistrer(
    destinataire: str,
    template_code: str,
    sujet: str,
    *,
    membre_id: str | None = None,
    langue: str = "fr",
    priorite: str = "normale",
    contexte: dict[str, Any] | None = None,
    reference: str = "",
    cree_par: str | None = None,
    role: str | None = None,
) -> str | None:
    """Write down the intent to send, before it leaves. Returns the outbox id.

    A repeat of the same intent reopens the existing row instead of failing. The key
    is stable per recipient, template and reference, and a unique index enforces it,
    so a deliberate resend of a stuck invitation used to violate that index, be
    swallowed here, and leave the ledger with no trace of the very send an
    administrator had just ordered. The row now carries the whole story: how many
    attempts, and how the last one ended.
    """
    try:
        row = db.execute(
            "INSERT INTO email_outbox (membre_id, destinataire, template_code, langue, sujet, "
            "contexte, priorite, statut, cle_idempotence, cree_par) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'en_cours', %s, %s) "
            "ON CONFLICT (cle_idempotence) DO UPDATE SET "
            "  statut = 'en_cours', sujet = EXCLUDED.sujet, contexte = EXCLUDED.contexte, "
            "  erreur = NULL, echoue_le = NULL, cree_le = now() "
            "RETURNING id",
            (membre_id, destinataire, template_code, langue, sujet,
             json.dumps(contexte or {}), priorite,
             cle_idempotence(destinataire, template_code, reference), cree_par),
            role=role,
        )
        return str(row["id"]) if row else None
    except Exception:  # noqa: BLE001 - the ledger must never block a send
        return None


def marquer_resultat(
    outbox_id: str | None,
    envoye: bool,
    fournisseur: str,
    erreur: str | None = None,
    role: str | None = None,
) -> None:
    """Record how the attempt ended, as the provider answered it."""
    if not outbox_id:
        return
    try:
        db.execute(
            "UPDATE email_outbox SET statut = %s, fournisseur = %s, erreur = %s, "
            "tentatives = tentatives + 1, "
            "envoye_le = CASE WHEN %s THEN now() ELSE envoye_le END, "
            "echoue_le = CASE WHEN %s THEN NULL ELSE now() END "
            "WHERE id = %s",
            ("envoye" if envoye else "echoue", fournisseur, erreur, envoye, envoye, outbox_id),
            role=role,
        )
    except Exception:  # noqa: BLE001
        pass


def enregistrer_evenement(
    destinataire: str,
    evenement: str,
    *,
    outbox_id: str | None = None,
    motif: str | None = None,
    charge: dict[str, Any] | None = None,
    survenu_le: str | None = None,
    role: str | None = None,
) -> None:
    """Record what the provider reported after the fact, and move the outbox along.

    Delivery, opening, bouncing and rejection all happen after the request was
    accepted, which is exactly the window where the platform used to be blind.

    ``survenu_le`` is when the provider says the event happened, which is not when we
    hear about it. Callbacks arrive late, and a history imported in bulk arrives all
    at once: dating every row at insertion time would compress weeks of bounces into
    one apparent incident, and the mailbox diagnosis reads exactly that column to
    decide whether an address is refusing mail *now*. Defaults to now() when the
    provider gives no date, so the column is never null and stays indexable.
    """
    statut = _STATUTS_FOURNISSEUR.get(evenement, "en_cours")
    try:
        db.execute(
            "INSERT INTO email_delivery_event (email_outbox_id, destinataire, evenement_fournisseur, "
            "statut_normalise, motif, charge_brute, survenu_le) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s::timestamptz, now()))",
            (outbox_id, destinataire, evenement, statut, motif, json.dumps(charge or {}), survenu_le),
            role=role,
        )
    except Exception:  # noqa: BLE001
        return
    if not outbox_id:
        return
    colonnes = {"delivre": "delivre_le", "ouvert": "ouvert_le", "rebondi": "echoue_le", "rejete": "echoue_le"}
    colonne = colonnes.get(statut)
    try:
        if colonne:
            db.execute(
                f"UPDATE email_outbox SET statut = %s, {colonne} = COALESCE({colonne}, now()) WHERE id = %s",
                (statut, outbox_id), role=role,
            )
    except Exception:  # noqa: BLE001
        pass


# What a provider says when a mailbox itself is the obstacle, mapped to what the
# member has to do about it. Matched on the lowered reason, most specific first.
# Anything else is reported as a refusal without a guessed cause: an invented
# explanation would send the member chasing the wrong fix.
_MOTIFS_BOITE = (
    ("out of storage", "la boîte de réception est pleine"),
    ("quota", "la boîte de réception a atteint son quota"),
    ("mailbox full", "la boîte de réception est pleine"),
    ("insufficient system storage", "le serveur du destinataire manque d'espace"),
    ("user unknown", "l'adresse n'existe pas chez le fournisseur"),
    ("does not exist", "l'adresse n'existe pas chez le fournisseur"),
)


def diagnostic_boite(destinataire: str, heures: int = 48, role: str | None = None) -> dict:
    """Has this mailbox been refusing our messages lately, and why.

    A member could not sign in and had no way to know why: the screen announced a
    code while the provider bounced every message for two days. The events were
    recorded all along; nothing read them back. This does, and answers in terms the
    member can act on.

    Reads only the recent window, on the dated column, so an address that bounced
    once last month is not held against a mailbox that works today.

    This is observability. It can never raise: a diagnostic that breaks a login is
    worse than a login with no diagnostic.
    """
    vide = {"probleme": False, "motif": "", "explication": "", "occurrences": 0}
    if not destinataire:
        return vide
    try:
        lignes = db.fetch_all(
            "SELECT motif, survenu_le FROM email_delivery_event "
            "WHERE lower(destinataire) = lower(%s) "
            "AND statut_normalise IN ('rebondi', 'rejete') "
            "AND survenu_le > now() - (%s || ' hours')::interval "
            "ORDER BY survenu_le DESC LIMIT 20",
            (destinataire, str(int(heures))),
            role=role,
        )
    except Exception:  # noqa: BLE001 - never let a diagnostic break a login
        return vide
    if not lignes:
        return vide

    motif = str((lignes[0] or {}).get("motif") or "").strip()
    bas = motif.lower()
    explication = "le fournisseur de messagerie a refusé les derniers envois"
    for marqueur, phrase in _MOTIFS_BOITE:
        if marqueur in bas:
            explication = phrase
            break
    return {"probleme": True, "motif": motif[:300],
            "explication": explication, "occurrences": len(lignes)}
