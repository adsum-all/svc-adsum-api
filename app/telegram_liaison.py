"""Tell the member where their Telegram link stands, and let them undo it.

Linking worked, but the member could not see it working. There was no way to ask
"am I linked, and to what", no way to tell a link that was waiting for a code from
one that had expired, and no way to unlink at all: a member who had changed phone or
Telegram account was stuck with a binding they could not undo, which is both a usage
dead end and a data-protection problem.

These three states are what a guided flow needs to show the member what to do next,
which is exactly what the interface could not do before.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from . import audit, channels, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["notifications"])


@router.get("/telegram")
def etat_liaison(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, Any]:
    """Where the member's Telegram link stands right now.

    The three states are distinct on purpose: linked, waiting for the code the bot
    just sent, and nothing in progress. An interface that cannot tell them apart
    cannot tell the member what to do next, which is what made the flow opaque.
    """
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non rattaché à un membre")
    row = db.fetch_one(
        "SELECT (telegram_chat_id IS NOT NULL) AS liee, "
        "(telegram_pending_chat_id IS NOT NULL AND telegram_confirm_expire > now()) AS en_attente, "
        "GREATEST(0, EXTRACT(epoch FROM (telegram_confirm_expire - now())))::int AS secondes_restantes, "
        "COALESCE(telegram_confirm_essais, 0) AS essais, "
        "(telegram_link_token IS NOT NULL) AS lien_demande "
        "FROM membre WHERE id = %s",
        (user.membre_id,), role=user.role,
    ) or {}
    en_attente = bool(row.get("en_attente"))
    return {
        # Whether the service is usable at all: no bot configured means the member
        # should be told so rather than shown a button that cannot work.
        "disponible": bool(channels.telegram_username()),
        "liee": bool(row.get("liee")),
        "en_attente_de_code": en_attente,
        "secondes_restantes": int(row.get("secondes_restantes") or 0) if en_attente else 0,
        # Five attempts is the ceiling enforced when confirming; showing what is
        # left avoids a member burning the last one without warning.
        "essais_restants": max(0, 5 - int(row.get("essais") or 0)) if en_attente else 5,
        "lien_demande": bool(row.get("lien_demande")),
    }


@router.delete("/telegram")
def delier(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, Any]:
    """Undo the link, and stop sending anything to that chat.

    The notification preference is turned off in the same move: leaving it on would
    keep the member marked as reachable on a channel that no longer exists, which is
    how a message ends up going nowhere without anyone noticing.
    """
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non rattaché à un membre")
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE membre SET telegram_chat_id = NULL, telegram_pending_chat_id = NULL, "
            "telegram_confirm_code = NULL, telegram_confirm_expire = NULL, "
            "telegram_confirm_essais = 0, telegram_link_token = NULL "
            "WHERE id = %s RETURNING id",
            (user.membre_id,),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membre introuvable")
        cur.execute(
            "UPDATE preference_notification SET telegram = false WHERE membre_id = %s",
            (user.membre_id,),
        )
    audit.log(user.id, user.role, "liaison_telegram_supprimee", "membre", user.membre_id, {})
    return {"liee": False}
