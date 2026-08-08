"""Inbound mail: a reply by e-mail lands in the thread it answers.

Support that only works inside the application is half a support. A person answers the
message they received, in their mail client, and if that reply is not attached to the
conversation it disappears: the agent sees a thread with no answer and closes it, while
the requester sees a question they answered and nobody read.

Attachment follows two signals, in order of reliability.

The **reference** carried in the subject (``[SUP-2026-0042]``) is deliberate and
survives a client rewriting the rest of the line. The **sender address** matched against
an open thread is the fallback for a reply whose subject was retyped. When neither
matches, the message opens a new thread rather than being dropped: an unattached message
is a nuisance, a lost one is a person left without an answer.

De-duplication uses the provider's ``Message-ID``, enforced by a unique index rather
than a lookup, because two simultaneous deliveries of the same message would both pass a
lookup and both insert.

The endpoint is provider-agnostic: it accepts the field names used by the common inbound
parsers rather than one vendor's shape, so pointing a different mailbox at it is a
configuration change. It is protected by the same shared secret as the delivery webhook,
and an empty secret accepts nothing: an open inbox endpoint is an open door for anyone
to write into the support queue.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from . import channels, db
from .support import STATUTS_OUVERTS, _reference

router = APIRouter(prefix="/api/v1", tags=["support"])

_REFERENCE = re.compile(r"SUP-\d{4}-\d{4,}", re.IGNORECASE)
#: Quoted history and signatures, which turn a two-line answer into a wall of text.
_SEPARATEURS = (
    re.compile(r"^\s*-{2,}\s*$", re.MULTILINE),
    # The accent is optional: a client that strips it, or a sender typing without
    # one, still produces the marker, and missing it leaves the whole quoted history
    # inside the exchange.
    re.compile(r"^\s*Le .{0,80} a [ée]crit\s*:\s*$", re.MULTILINE),
    re.compile(r"^\s*On .{0,80} wrote\s*:\s*$", re.MULTILINE),
    re.compile(r"^\s*_{5,}\s*$", re.MULTILINE),
    re.compile(r"^\s*Envoy[ée] de mon .+$", re.MULTILINE),
    re.compile(r"^\s*Sent from my .+$", re.MULTILINE),
    re.compile(r"^\s*De\s*:\s.+$", re.MULTILINE),
    re.compile(r"^\s*From\s*:\s.+$", re.MULTILINE),
)


def _premier(charge: dict[str, Any], *noms: str) -> str:
    """First non-empty value among several spellings of the same field.

    Inbound parsers disagree on names: ``text`` and ``TextBody`` and ``plain`` all mean
    the body. Reading several keeps this endpoint usable with more than one provider.
    """
    for nom in noms:
        valeur = charge.get(nom)
        if isinstance(valeur, str) and valeur.strip():
            return valeur.strip()
        if isinstance(valeur, dict):
            for cle in ("address", "email", "Email", "value"):
                interne = valeur.get(cle)
                if isinstance(interne, str) and interne.strip():
                    return interne.strip()
        if isinstance(valeur, list) and valeur:
            return _premier({"x": valeur[0]}, "x")
    return ""


def _corps_utile(texte: str) -> str:
    """The answer, without the conversation it quotes back at us.

    Everything from the first quoting marker onwards is history the thread already
    holds. Keeping it makes each exchange longer than the last and buries the two
    sentences that matter.
    """
    coupe = len(texte)
    for motif in _SEPARATEURS:
        trouve = motif.search(texte)
        if trouve and trouve.start() < coupe:
            coupe = trouve.start()
    lignes = [ligne for ligne in texte[:coupe].splitlines() if not ligne.lstrip().startswith(">")]
    propre = "\n".join(lignes).strip()
    # A reply that is only quoted history still carries intent (often "thanks"), so
    # never return nothing: an empty body would fail the column's NOT NULL.
    return propre or texte.strip()[:4000] or "(message vide)"


def _fil_cible(reference: str, expediteur: str) -> dict[str, Any] | None:
    if reference:
        fil = db.fetch_one("SELECT id, statut FROM support_fil WHERE upper(reference) = upper(%s)", (reference,))
        if fil:
            return dict(fil)
    if expediteur:
        fil = db.fetch_one(
            "SELECT id, statut FROM support_fil WHERE lower(demandeur_email) = lower(%s) AND statut = ANY(%s) "
            "ORDER BY maj_le DESC LIMIT 1",
            (expediteur, list(STATUTS_OUVERTS)),
        )
        if fil:
            return dict(fil)
    return None


@router.post("/webhooks/support-entrant", status_code=status.HTTP_202_ACCEPTED)
async def recevoir_message_entrant(
    request: Request,
    x_adsum_webhook: Annotated[str | None, Header()] = None,
    cle: str = Query(default=""),
) -> dict[str, Any]:
    """Attach one inbound e-mail to a support thread, or open one.

    Answers 202 in every accepted case, including a duplicate: a provider that receives
    an error retries, and retrying a message already recorded would only produce more
    duplicates.
    """
    attendu = (channels.integration_value("email_webhook_secret") or "").strip()
    if not attendu:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Réception entrante non configurée : aucun secret partagé n'est défini.",
        )
    presente = (x_adsum_webhook or cle or "").strip()
    if presente != attendu:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="secret invalide")

    try:
        charge = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="corps JSON illisible") from None
    if not isinstance(charge, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="objet JSON attendu")
    return traiter_message_entrant(charge)


def traiter_message_entrant(charge: dict[str, Any]) -> dict[str, Any]:
    """Attach one already-authenticated inbound message, or open a thread for it.

    Separate from the endpoint so the attachment rules can be exercised without a
    shared secret. Planting a weak secret in a live configuration to run a test leaves
    a real credential behind if the run stops halfway, which is a worse risk than the
    one the test was checking.
    """
    # Some parsers wrap the message; unwrap one level rather than refusing.
    for enveloppe in ("message", "email", "Message"):
        interne = charge.get(enveloppe)
        if isinstance(interne, dict):
            charge = {**charge, **interne}

    expediteur = _premier(charge, "from", "From", "sender", "Sender", "from_email").lower()
    sujet = _premier(charge, "subject", "Subject") or "(sans objet)"
    corps = _premier(charge, "text", "TextBody", "plain", "body", "Body", "html", "HtmlBody")
    message_id = _premier(charge, "message_id", "MessageID", "Message-Id", "messageId")
    nom = _premier(charge, "from_name", "FromName", "sender_name")

    if not expediteur:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="expéditeur absent")

    trouve = _REFERENCE.search(sujet) or _REFERENCE.search(corps or "")
    reference = trouve.group(0).upper() if trouve else ""
    fil = _fil_cible(reference, expediteur)

    if fil is None:
        nouvelle = _reference()
        cree = db.execute(
            "INSERT INTO support_fil (reference, sujet, statut, categorie, canal, demandeur_email, demandeur_nom, "
            "  demandeur_utilisateur_id) "
            "VALUES (%s, %s, 'nouveau', 'autre', 'email', %s, %s, "
            "  (SELECT id FROM utilisateur WHERE lower(email) = lower(%s) LIMIT 1)) RETURNING id",
            (nouvelle, sujet[:160], expediteur, nom or None, expediteur),
        )
        if not cree:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="création impossible")
        fil = {"id": cree["id"], "statut": "nouveau"}
        reference = nouvelle

    # The unique partial index is what actually prevents a duplicate: a prior lookup
    # would let two simultaneous deliveries both pass.
    insere = db.execute(
        "INSERT INTO support_message (fil_id, entrant, auteur_nom, auteur_email, corps, message_id) "
        "VALUES (%s, true, %s, %s, %s, %s) "
        "ON CONFLICT (message_id) WHERE message_id IS NOT NULL DO NOTHING RETURNING id",
        (fil["id"], nom or expediteur, expediteur, _corps_utile(corps or ""), message_id or None),
    )
    if insere is None:
        # The duplicate still names its thread. Returning an empty reference told the
        # provider nothing, and made a legitimate retry indistinguishable from a
        # message that had landed nowhere.
        connu = db.fetch_one("SELECT reference FROM support_fil WHERE id = %s", (fil["id"],))
        return {
            "recu": True,
            "duplique": True,
            "reference": str(connu["reference"]) if connu else reference,
            "fil": str(fil["id"]),
        }

    # A closed thread that receives an answer is not closed: the person is still
    # talking. Reopening beats silently appending to something nobody will read again.
    db.execute(
        "UPDATE support_fil SET statut = CASE WHEN statut IN ('resolu', 'clos') THEN 'en_cours' "
        "  WHEN statut = 'en_attente' THEN 'en_cours' ELSE statut END, "
        "ferme_le = CASE WHEN statut IN ('resolu', 'clos') THEN NULL ELSE ferme_le END, "
        "maj_le = now() WHERE id = %s",
        (fil["id"],),
    )
    return {"recu": True, "duplique": False, "reference": reference, "fil": str(fil["id"])}


@router.get("/admin/support/adresse-entrante")
def adresse_entrante(cle: str = Query(default="")) -> dict[str, object]:
    """The exact address to configure at the provider, secret included.

    Published as an endpoint rather than written in a runbook, because a runbook goes
    stale and an administrator then guesses the URL.
    """
    attendu = (channels.integration_value("email_webhook_secret") or "").strip()
    if not attendu or cle.strip() != attendu:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="secret invalide")
    return {
        "url": "/api/v1/webhooks/support-entrant",
        "entete": "X-Adsum-Webhook",
        "note": (
            "Configurez la redirection entrante de votre boîte de support vers cette adresse, "
            "en présentant le secret dans l'en-tête indiqué ou en paramètre cle."
        ),
    }
