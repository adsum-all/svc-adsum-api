"""The support side of a conversation: take it, answer it, close it.

Everything here is bounded by one rule, stated in the permission itself: a support
agent answers a person, they do not open that person's file. A thread carries a name,
an address and what was written. It offers no path to the member record, the health
information, the documents or the attendance history, and this module deliberately
never joins to them.

An answer is sent for real and the outcome is stored. A reply that never left is
visible as a failure on the exchange itself, rather than sitting in a list looking
answered. That distinction is the whole point of a support tool: an unanswered
request and a request whose answer bounced look identical until someone records the
difference.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe
from .support import _SELECT_FIL, PRIORITES, STATUTS, STATUTS_OUVERTS, _fil_out, _messages

router = APIRouter(prefix="/api/v1/support/console", tags=["support-console"])


@router.get("/fils")
def lister(
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
    statut: str = Query(default="ouverts", description="ouverts, tous, ou un statut précis"),
    assigne: str = Query(default="", description="Identifiant d'un agent, ou 'moi', ou 'personne'"),
    recherche: str = Query(default="", max_length=120),
    decalage: int = Query(default=0, ge=0),
    limite: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """The queue, filtered and paginated, newest activity first.

    Ordered by last activity rather than creation: a thread nobody has touched for a
    week matters more than one opened an hour ago and already answered.
    """
    conditions: list[str] = []
    params: list[object] = []

    if statut == "ouverts":
        conditions.append("f.statut = ANY(%s)")
        params.append(list(STATUTS_OUVERTS))
    elif statut in STATUTS:
        conditions.append("f.statut = %s")
        params.append(statut)
    elif statut != "tous":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"statut inconnu : {statut}")

    if assigne == "moi":
        conditions.append("f.assigne_a = %s")
        params.append(user.id)
    elif assigne == "personne":
        conditions.append("f.assigne_a IS NULL")
    elif assigne:
        conditions.append("f.assigne_a = %s")
        params.append(assigne)

    if recherche.strip():
        # Searches what a support agent actually remembers: the reference they were
        # given, or a word from the subject. Not the body, which would surface
        # unrelated threads on a common word and slow the queue for no gain.
        conditions.append("(f.reference ILIKE %s OR f.sujet ILIKE %s OR f.demandeur_email ILIKE %s)")
        motif = f"%{recherche.strip()}%"
        params.extend([motif, motif, motif])

    ou = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    total_row = db.fetch_one(f"SELECT count(*) AS n FROM support_fil f{ou}", tuple(params), role=user.role)
    total = int(total_row["n"]) if total_row else 0

    rows = db.fetch_all(
        _SELECT_FIL + ou + " ORDER BY f.maj_le DESC OFFSET %s LIMIT %s",
        tuple([*params, decalage, limite]),
        role=user.role,
    )
    return {"total": total, "decalage": decalage, "limite": limite, "fils": [_fil_out(r) for r in rows]}


@router.get("/synthese")
def synthese(user: Annotated[UserMe, Depends(require_permission("support.traiter"))]) -> dict[str, object]:
    """What the queue looks like right now, and what is aging in it.

    The oldest unanswered thread is reported on purpose: an average response time hides
    the one request that has been waiting three weeks, and that is the one that costs
    an organisation its trust.
    """
    par_statut = {
        str(r["statut"]): int(r["n"])
        for r in db.fetch_all("SELECT statut, count(*) AS n FROM support_fil GROUP BY 1", (), role=user.role)
    }
    attente = db.fetch_one(
        "SELECT count(*) AS n, "
        "  round(extract(epoch FROM (now() - min(f.cree_le))) / 3600) AS plus_ancienne_heures "
        "FROM support_fil f WHERE f.statut = ANY(%s) AND f.derniere_reponse_le IS NULL",
        (list(STATUTS_OUVERTS),),
        role=user.role,
    ) or {}
    non_assignes = db.fetch_one(
        "SELECT count(*) AS n FROM support_fil WHERE assigne_a IS NULL AND statut = ANY(%s)",
        (list(STATUTS_OUVERTS),),
        role=user.role,
    ) or {}
    echecs = db.fetch_one(
        "SELECT count(*) AS n FROM support_message WHERE NOT entrant AND NOT envoye", (), role=user.role
    ) or {}
    return {
        "par_statut": {s: par_statut.get(s, 0) for s in STATUTS},
        "ouverts": sum(par_statut.get(s, 0) for s in STATUTS_OUVERTS),
        "jamais_repondus": int(attente.get("n") or 0),
        "plus_ancienne_attente_heures": int(attente.get("plus_ancienne_heures") or 0),
        "non_assignes": int(non_assignes.get("n") or 0),
        "reponses_non_parties": int(echecs.get("n") or 0),
    }


def _charger(fil_id: str, role: str | None) -> dict[str, object]:
    fil = db.fetch_one(_SELECT_FIL + "WHERE f.id = %s", (fil_id,), role=role)
    if not fil:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Demande introuvable.")
    return fil


@router.get("/fils/{fil_id}")
def lire(fil_id: str, user: Annotated[UserMe, Depends(require_permission("support.traiter"))]) -> dict[str, object]:
    fil = _charger(fil_id, user.role)
    return {**_fil_out(fil), "echanges": _messages(fil_id, user.role)}


class MajFil(BaseModel):
    statut: str | None = None
    priorite: str | None = None
    #: Identifier of the agent, "moi" to take it, or empty string to release it.
    assigne_a: str | None = None


@router.patch("/fils/{fil_id}")
def mettre_a_jour(
    fil_id: str,
    payload: MajFil,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> dict[str, object]:
    """Change state, priority or owner.

    Closing stamps the closing time in the same statement as the status, because the
    table refuses one without the other: a closed thread with no closing date would
    make retention unable to find it.
    """
    _charger(fil_id, user.role)
    champs: list[str] = []
    valeurs: list[object] = []

    if payload.statut is not None:
        if payload.statut not in STATUTS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"statut inconnu : {payload.statut}")
        champs.append("statut = %s")
        valeurs.append(payload.statut)
        champs.append("ferme_le = CASE WHEN %s IN ('resolu', 'clos') THEN coalesce(ferme_le, now()) ELSE NULL END")
        valeurs.append(payload.statut)

    if payload.priorite is not None:
        if payload.priorite not in PRIORITES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"priorité inconnue : {payload.priorite}")
        champs.append("priorite = %s")
        valeurs.append(payload.priorite)

    if payload.assigne_a is not None:
        cible = user.id if payload.assigne_a == "moi" else (payload.assigne_a or None)
        if cible:
            existe = db.fetch_one("SELECT id FROM utilisateur WHERE id = %s AND actif", (cible,), role=user.role)
            if not existe:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent inconnu ou inactif.")
        champs.append("assigne_a = %s")
        valeurs.append(cible)

    if not champs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune modification demandée.")

    valeurs.append(fil_id)
    db.execute(f"UPDATE support_fil SET {', '.join(champs)}, maj_le = now() WHERE id = %s", tuple(valeurs), role=user.role)
    audit.log(user.id, user.role, "maj_fil_support", "support_fil", fil_id, payload.model_dump(exclude_none=True))
    return _fil_out(_charger(fil_id, user.role))


class ReponseConsole(BaseModel):
    message: str = Field(min_length=2, max_length=8000)
    #: Close the thread with this answer, when the answer settles it.
    clore: bool = False


@router.post("/fils/{fil_id}/reponses", status_code=status.HTTP_201_CREATED)
def repondre(
    fil_id: str,
    payload: ReponseConsole,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> dict[str, object]:
    """Answer the requester, by e-mail, and record whether it actually left.

    The exchange is written before the send is attempted and updated afterwards. If the
    process dies mid-send the answer still exists and is visibly unsent, which is
    recoverable. Writing it only on success would lose the text entirely.
    """
    from .email_gateway import send_email
    from .email_templates import render_notification_email

    fil = _charger(fil_id, user.role)
    destinataire = str(fil["demandeur_email"])

    exchange = db.execute(
        "INSERT INTO support_message (fil_id, entrant, auteur_utilisateur_id, auteur_nom, auteur_email, corps, envoye) "
        "VALUES (%s, false, %s, %s, %s, %s, false) RETURNING id",
        (fil_id, user.id, str(user.email), str(user.email), payload.message.strip()),
        role=user.role,
    )
    if not exchange:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Enregistrement impossible.")

    # The reference travels in the subject so the requester's reply comes back to this
    # thread instead of opening a new one.
    sujet = f"[{fil['reference']}] {fil['sujet']}"
    html = render_notification_email(str(fil["sujet"]), payload.message.strip())
    envoye, fournisseur = send_email(destinataire, sujet, payload.message.strip(), html)

    db.execute(
        "UPDATE support_message SET envoye = %s, erreur_envoi = %s WHERE id = %s",
        (envoye, None if envoye else f"non remis par {fournisseur}", exchange["id"]),
        role=user.role,
    )
    nouveau_statut = "clos" if payload.clore else "en_attente"
    db.execute(
        "UPDATE support_fil SET statut = %s, ferme_le = CASE WHEN %s THEN coalesce(ferme_le, now()) ELSE NULL END, "
        "derniere_reponse_le = now(), maj_le = now(), assigne_a = coalesce(assigne_a, %s) WHERE id = %s",
        (nouveau_statut, payload.clore, user.id, fil_id),
        role=user.role,
    )
    audit.log(user.id, user.role, "repondre_support", "support_fil", fil_id, {"envoye": envoye, "clos": payload.clore})
    return {"ok": True, "envoye": envoye, "statut": nouveau_statut}


@router.get("/agents")
def agents(user: Annotated[UserMe, Depends(require_permission("support.traiter"))]) -> list[dict[str, str]]:
    """Who a thread can be assigned to: the accounts that hold the permission.

    Listing every account would offer assignments that cannot act on what they receive.
    """
    from .permissions_data import ROLE_PERMISSIONS

    roles = [r for r, p in ROLE_PERMISSIONS.items() if "support.traiter" in p]
    return [
        {"id": str(r["id"]), "email": str(r["email"])}
        for r in db.fetch_all(
            "SELECT id, email FROM utilisateur WHERE actif AND role = ANY(%s) ORDER BY email",
            (roles,),
            role=user.role,
        )
    ]
