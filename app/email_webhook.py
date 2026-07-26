"""Receive what the mail provider reports after a message has left.

The platform used to know one thing: the provider accepted the request. Everything
that happens next, delivery, opening, bouncing, a mailbox refusing the address, was
invisible, so an administrator could not tell a message that arrived from one that
was rejected. On this base one address soft-bounces every single time, and nothing in
the interface said so.

This endpoint closes that gap. The provider calls it, the event lands in the delivery
ledger, and the outbox row moves to what actually happened.

The address is matched case-insensitively on purpose: providers normalise recipients
to lower case, so a member registered as PhilJoeK@gmail.com comes back as
philjoek@gmail.com. Comparing them literally is how a delivered message ends up
looking like a message that was never sent.
"""
# ruff: noqa: E501
from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from . import channels, db, email_registre
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["email"])

# Provider event names that concern one message. Anything else (contact list changes,
# marketing unsubscribes) is acknowledged and ignored rather than misfiled.
_EVENEMENTS_MESSAGE = {
    "request", "delivered", "opened", "uniqueOpened", "click",
    "soft_bounce", "hard_bounce", "softBounces", "hardBounces",
    "blocked", "spam", "invalid_email", "deferred", "error", "unsubscribed",
}

# Provider spellings normalised to what the ledger understands.
_ALIAS = {
    "soft_bounce": "softBounces",
    "hard_bounce": "hardBounces",
    "invalid_email": "invalid",
    "unique_opened": "uniqueOpened",
}


def _secret_attendu() -> str:
    """Shared secret the provider must present. Empty means the check is off."""
    return (channels.integration_value("email_webhook_secret") or "").strip()


@router.post("/webhooks/email", status_code=status.HTTP_202_ACCEPTED)
async def recevoir_evenement_email(
    request: Request,
    x_adsum_webhook: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Record one delivery event reported by the mail provider.

    Always answers 202 once authenticated: a provider that receives an error retries,
    and a retry storm over a row that could not be written helps nobody. What could not
    be recorded is reported in the body instead.
    """
    secret = _secret_attendu()
    if secret and not hmac.compare_digest(secret, x_adsum_webhook or ""):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="signature de webhook invalide")

    try:
        charge = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is not worth a retry storm
        return {"recu": False, "motif": "corps illisible"}
    if not isinstance(charge, dict):
        return {"recu": False, "motif": "format inattendu"}

    brut = str(charge.get("event") or charge.get("type") or "").strip()
    evenement = _ALIAS.get(brut, brut)
    destinataire = str(charge.get("email") or charge.get("recipient") or "").strip()
    if not evenement or not destinataire:
        return {"recu": False, "motif": "événement ou destinataire absent"}
    if evenement not in _EVENEMENTS_MESSAGE:
        return {"recu": True, "ignore": True, "motif": f"événement {evenement} hors périmètre"}

    # Case-insensitive match: providers lower-case the recipient, and comparing
    # literally is exactly how a delivered message looks like one never sent.
    ligne = db.fetch_one(
        "SELECT id FROM email_outbox WHERE lower(destinataire) = lower(%s) "
        "ORDER BY cree_le DESC LIMIT 1",
        (destinataire,),
    )
    outbox_id = str(ligne["id"]) if ligne else None

    motif = charge.get("reason") or charge.get("message") or None
    email_registre.enregistrer_evenement(
        destinataire, evenement, outbox_id=outbox_id,
        motif=str(motif)[:500] if motif else None, charge=charge,
    )
    return {"recu": True, "evenement": evenement, "rattache": bool(outbox_id)}


@router.get("/admin/email/sante")
def sante_delivrabilite(
    user: Annotated[UserMe, Depends(require_permission("integrations.superviser"))],
) -> dict[str, Any]:
    """How the sending is actually doing, over the last thirty days.

    Deliberately expressed as counts an administrator can act on rather than rates
    alone: one address bouncing every time is a person who never hears from the
    organisation, and that matters more than a percentage looking acceptable.
    """
    stats = db.fetch_all(
        "SELECT statut, COUNT(*) AS n FROM email_outbox "
        "WHERE cree_le > now() - interval '30 days' GROUP BY statut",
        (), role=user.role,
    )
    par_statut = {str(r["statut"]): int(r["n"]) for r in stats}
    total = sum(par_statut.values())

    problemes = db.fetch_all(
        "SELECT lower(destinataire) AS adresse, COUNT(*) AS n, max(survenu_le) AS dernier "
        "FROM email_delivery_event WHERE statut_normalise IN ('rebondi', 'rejete') "
        "AND survenu_le > now() - interval '30 days' "
        "GROUP BY 1 ORDER BY n DESC LIMIT 20",
        (), role=user.role,
    )
    return {
        "periode": "30 derniers jours",
        "total_envois": total,
        "par_statut": par_statut,
        "adresses_en_echec": [
            {"adresse": r["adresse"], "echecs": int(r["n"]), "dernier": r["dernier"]}
            for r in problemes
        ],
        "alerte": len(problemes) > 0,
    }
