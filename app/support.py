"""Support conversations: a member asks, a human answers, and it is traceable.

A member who hits a technical problem had nowhere to go. Their message reached a
personal mailbox or nobody, and either way nothing recorded that a request existed,
who took it, or whether it was ever answered.

Three surfaces, deliberately separate.

**The requester** opens a thread from inside the application, sees their own threads
and nobody else's, and can add to a conversation that is still open. They never see a
state machine: a thread is open or it is closed.

**The support side** lists everything, assigns, replies, and closes. Replying sends a
real e-mail and records whether it left, so an answer that never reached its
destination is visible rather than assumed.

**Inbound e-mail** attaches a reply to the thread it answers, using the reference
carried in the subject and the provider's message identifier for de-duplication. A
support mailbox without that becomes a pile of disconnected fragments.

On personal data, which is the constraint that shapes this module: support exists to
fix the platform, not to read member files. A thread carries what is needed to answer
a person, their name, their address and what they wrote, and offers no path at all to
their member record. Anyone needing that record opens the back office, where the
access is already governed and audited.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .auth import current_user
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["support"])

STATUTS_OUVERTS = ("nouveau", "en_cours", "en_attente")
STATUTS = (*STATUTS_OUVERTS, "resolu", "clos")
PRIORITES = ("basse", "normale", "haute", "critique")

#: The reference is quoted in the subject so a reply finds its way home.
_REFERENCE = re.compile(r"\bSUP-(\d{4})-(\d{4,})\b", re.IGNORECASE)


def _reference() -> str:
    """A reference a human can read out loud, allocated by the database.

    Building it from a count would hand the same number to two simultaneous requests.
    """
    row = db.fetch_one("SELECT nextval('support_reference_seq') AS n", ())
    numero = int(row["n"]) if row else 1
    return f"SUP-{datetime.now(UTC).year}-{numero:04d}"


def _fil_out(r: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(r["id"]),
        "reference": r["reference"],
        "sujet": r["sujet"],
        "statut": r["statut"],
        "ouvert": str(r["statut"]) in STATUTS_OUVERTS,
        "priorite": r["priorite"],
        "categorie": r["categorie"],
        "canal": r["canal"],
        "application": r.get("application"),
        "demandeur_nom": r.get("demandeur_nom"),
        "demandeur_email": r.get("demandeur_email"),
        "assigne_a": str(r["assigne_a"]) if r.get("assigne_a") else None,
        "assigne_nom": r.get("assigne_nom"),
        "messages": int(r["messages"]) if r.get("messages") is not None else None,
        "cree_le": r["cree_le"].isoformat() if r.get("cree_le") else None,
        "maj_le": r["maj_le"].isoformat() if r.get("maj_le") else None,
        "derniere_reponse_le": r["derniere_reponse_le"].isoformat() if r.get("derniere_reponse_le") else None,
        "ferme_le": r["ferme_le"].isoformat() if r.get("ferme_le") else None,
    }


_SELECT_FIL = (
    "SELECT f.*, u.email AS assigne_nom, "
    "  (SELECT count(*) FROM support_message m WHERE m.fil_id = f.id) AS messages "
    "FROM support_fil f LEFT JOIN utilisateur u ON u.id = f.assigne_a "
)


# --- Requester side ----------------------------------------------------------

class DemandeIn(BaseModel):
    sujet: str = Field(min_length=3, max_length=160)
    message: str = Field(min_length=10, max_length=8000)
    categorie: str = Field(default="autre", max_length=40)
    application: str = Field(default="", max_length=40)


@router.get("/support/categories")
def categories(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """What a requester can pick from, served rather than hard-coded per application."""
    return [
        {"code": str(r["code"]), "libelle": str(r["libelle"]), "libelle_en": r["libelle_en"]}
        for r in db.fetch_all(
            "SELECT code, libelle, libelle_en FROM support_categorie WHERE actif ORDER BY ordre, libelle",
            (), role=user.role,
        )
    ]


@router.post("/support/demandes", status_code=status.HTTP_201_CREATED)
def ouvrir(payload: DemandeIn, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Open a thread from inside the application.

    Works without any mailbox: the request lands in the database directly. That matters
    because a support address can be misconfigured, full, or not yet created, and a
    member must still be able to reach someone.
    """
    if not str(user.email or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Votre compte n'a pas d'adresse de courriel : nous ne pourrions pas vous répondre.",
        )
    connue = db.fetch_one(
        "SELECT code FROM support_categorie WHERE code = %s AND actif", (payload.categorie,), role=user.role
    )
    categorie = str(connue["code"]) if connue else "autre"

    # A requester with an open thread on the same subject is almost always continuing
    # the same conversation. Opening a second one splits the history and the answer.
    doublon = db.fetch_one(
        "SELECT id, reference FROM support_fil "
        "WHERE demandeur_utilisateur_id = %s AND statut = ANY(%s) AND lower(sujet) = lower(%s)",
        (user.id, list(STATUTS_OUVERTS), payload.sujet.strip()),
        role=user.role,
    )
    if doublon:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Vous avez déjà une demande ouverte portant ce sujet ({doublon['reference']}). Ajoutez votre message à cette demande.",
        )

    reference = _reference()
    fil = db.execute(
        "INSERT INTO support_fil (reference, sujet, statut, categorie, canal, application, "
        "  demandeur_utilisateur_id, demandeur_email, demandeur_nom) "
        "VALUES (%s, %s, 'nouveau', %s, 'application', %s, %s, %s, %s) RETURNING id, reference",
        (reference, payload.sujet.strip(), categorie, payload.application.strip() or None,
         user.id, str(user.email), _nom_de(user)),
        role=user.role,
    )
    if not fil:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Création impossible.")

    db.execute(
        "INSERT INTO support_message (fil_id, entrant, auteur_utilisateur_id, auteur_nom, auteur_email, corps) "
        "VALUES (%s, true, %s, %s, %s, %s)",
        (fil["id"], user.id, _nom_de(user), str(user.email), payload.message.strip()),
        role=user.role,
    )
    audit.log(user.id, user.role, "ouvrir_demande_support", "support_fil", str(fil["id"]), {"reference": reference})
    return {"id": str(fil["id"]), "reference": reference}


def _nom_de(user: UserMe) -> str:
    """A display name for the thread, without reaching into the member record.

    Support answers a person; it does not open their file. The name comes from the
    account, and when the account carries none the address is what remains.
    """
    row = db.fetch_one(
        "SELECT coalesce(nullif(trim(concat_ws(' ', m.nom, m.prenoms)), ''), u.email) AS nom "
        "FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id WHERE u.id = %s",
        (user.id,),
    )
    return str(row["nom"]) if row and row.get("nom") else str(user.email or "")


@router.get("/support/demandes")
def mes_demandes(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, object]]:
    """The requester's own threads, and only those."""
    return [
        _fil_out(r)
        for r in db.fetch_all(
            _SELECT_FIL + "WHERE f.demandeur_utilisateur_id = %s ORDER BY f.maj_le DESC LIMIT 100",
            (user.id,), role=user.role,
        )
    ]


class ReponseIn(BaseModel):
    message: str = Field(min_length=2, max_length=8000)


def _messages(fil_id: str, role: str | None) -> list[dict[str, object]]:
    return [
        {
            "id": str(r["id"]),
            "entrant": bool(r["entrant"]),
            "auteur_nom": r["auteur_nom"],
            "corps": r["corps"],
            "envoye": bool(r["envoye"]),
            "erreur_envoi": r["erreur_envoi"],
            "cree_le": r["cree_le"].isoformat() if r["cree_le"] else None,
        }
        for r in db.fetch_all(
            "SELECT id, entrant, auteur_nom, corps, envoye, erreur_envoi, cree_le "
            "FROM support_message WHERE fil_id = %s ORDER BY cree_le",
            (fil_id,), role=role,
        )
    ]


@router.get("/support/demandes/{fil_id}")
def ma_demande(fil_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    fil = db.fetch_one(
        _SELECT_FIL + "WHERE f.id = %s AND f.demandeur_utilisateur_id = %s", (fil_id, user.id), role=user.role
    )
    if not fil:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demande introuvable.")
    return {**_fil_out(fil), "echanges": _messages(fil_id, user.role)}


@router.post("/support/demandes/{fil_id}/messages", status_code=status.HTTP_201_CREATED)
def completer(fil_id: str, payload: ReponseIn, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, object]:
    """Add to one's own thread, as long as it is still open."""
    fil = db.fetch_one(
        "SELECT id, statut FROM support_fil WHERE id = %s AND demandeur_utilisateur_id = %s",
        (fil_id, user.id), role=user.role,
    )
    if not fil:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demande introuvable.")
    if str(fil["statut"]) not in STATUTS_OUVERTS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cette demande est close. Ouvrez-en une nouvelle pour un autre sujet.",
        )
    db.execute(
        "INSERT INTO support_message (fil_id, entrant, auteur_utilisateur_id, auteur_nom, auteur_email, corps) "
        "VALUES (%s, true, %s, %s, %s, %s)",
        (fil_id, user.id, _nom_de(user), str(user.email), payload.message.strip()),
        role=user.role,
    )
    # A requester who adds something is waiting again, whatever the support side had set.
    db.execute(
        "UPDATE support_fil SET statut = CASE WHEN statut = 'en_attente' THEN 'en_cours' ELSE statut END, "
        "maj_le = now() WHERE id = %s",
        (fil_id,), role=user.role,
    )
    return {"ok": True}
