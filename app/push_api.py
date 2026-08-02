"""Let the mobile application say where to reach this member, and stop.

The application receives a token from the push service at first launch and again
whenever the service rotates it. It posts it here; the platform then knows a phone to
notify. Signing out posts the removal, so notifications stop reaching a device the
member no longer uses.

Everything is scoped to the caller. The member id comes from the token, never from
the request body: a device identifier that a caller could attach to somebody else's
account would let anyone redirect another member's notifications to their own phone.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import push
from .auth import current_user
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/membres/me", tags=["push"])

_PLATEFORMES = ("android", "ios", "web")


class AppareilIn(BaseModel):
    """A device registration. The member is taken from the token, not from here."""

    jeton: str = Field(min_length=8, max_length=4096)
    plateforme: str = Field(default="android")
    #: What the member sees in their list of devices. Their own words, so they can
    #: tell one phone from another when revoking.
    libelle: str | None = Field(default=None, max_length=80)


class AppareilOut(BaseModel):
    id: str
    plateforme: str
    libelle: str | None
    cree_le: str
    dernier_envoi: str | None


def _membre(user: UserMe) -> str:
    """The member behind the caller, or a refusal.

    A technical account carries no member row. It has nothing to notify on a phone,
    and saying so plainly is better than recording a device nothing will ever reach.
    """
    if not user.membre_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ce compte n'est pas rattaché à une fiche membre",
        )
    return str(user.membre_id)


@router.get("/appareils-push", response_model=list[AppareilOut])
def lister(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, Any]]:
    """The devices currently notified for this member."""
    return [
        {
            "id": str(a["id"]),
            "plateforme": str(a["plateforme"]),
            "libelle": a.get("libelle"),
            "cree_le": str(a["cree_le"]),
            "dernier_envoi": str(a["envoye_le"]) if a.get("envoye_le") else None,
        }
        for a in push.appareils(_membre(user), role=user.role)
    ]


@router.put("/appareils-push", status_code=status.HTTP_204_NO_CONTENT)
def enregistrer(payload: AppareilIn, user: Annotated[UserMe, Depends(current_user)]) -> None:
    """Register or refresh this device.

    PUT rather than POST: the application calls it at every launch, because the push
    service may have rotated the token while the application was closed. Repeating
    the same call must be the same as making it once.
    """
    if payload.plateforme not in _PLATEFORMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"plateforme inconnue (attendu : {', '.join(_PLATEFORMES)})",
        )
    if not push.enregistrer_appareil(
        _membre(user), payload.jeton, payload.plateforme, payload.libelle, role=user.role,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="enregistrement de l'appareil impossible",
        )


@router.delete("/appareils-push", status_code=status.HTTP_204_NO_CONTENT)
def retirer(payload: AppareilIn, user: Annotated[UserMe, Depends(current_user)]) -> None:
    """Stop notifying this device. Called on sign-out.

    The token is checked against the caller's own devices first. Without that, anyone
    holding a valid session could post another member's token and silence their
    notifications.
    """
    membre_id = _membre(user)
    siens = {str(a["jeton"]) for a in push.appareils(membre_id, role=user.role)}
    if payload.jeton not in siens:
        # Deliberately not a 404: whether a token exists on this platform is not
        # something an unrelated caller should be able to learn by probing.
        return
    push.retirer_appareil(payload.jeton, "déconnexion", role=user.role)


@router.get("/push-disponible")
def disponible(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, bool]:
    """Whether this deployment can deliver push at all.

    The application asks before requesting the notification permission from the
    operating system. A permission prompt for a channel the organisation has not
    configured is a prompt that buys the member nothing, and Android only offers it
    once.
    """
    return {"disponible": push.configure()}
