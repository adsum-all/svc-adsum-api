"""Member-facing two-factor authentication settings.

The member controls their own 2FA here: turn it on or off, pick the preferred code
channel (Telegram first with an e-mail fallback), and see or revoke the devices
they chose to trust. The login enforcement itself lives in ``auth.py``; these
endpoints only manage the stored state that enforcement reads. Every mutation is
audited. The account is never locked out from here: disabling 2FA is always
allowed, and the 6-month reminder is a soft flag, never a block.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["mfa"])

# After this long without opting in, the app shows the stronger (still gentle,
# still dismissible) reminder. Kept in one place so the cadence is explicit.
_NUDGE_AFTER_MONTHS = 6


class DoubleFacteurIn(BaseModel):
    actif: bool


@router.put("/membres/me/double-facteur")
def set_double_facteur(
    payload: DoubleFacteurIn,
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, object]:
    """Enable or disable the member's own two-factor authentication."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    # Stamp when 2FA was first recommended/enabled the first time, for the nudge
    # cadence; keep the existing stamp on later changes.
    db.execute(
        "UPDATE utilisateur SET mfa_actif = %s, "
        "mfa_recommande_le = COALESCE(mfa_recommande_le, now()) "
        "WHERE id = %s",
        (payload.actif, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "mfa_toggle", "utilisateur", user.id, {"actif": payload.actif})
    return {"ok": True, "actif": payload.actif}


class CanalIn(BaseModel):
    canal: str


@router.put("/membres/me/mfa-canal")
def set_mfa_canal(
    payload: CanalIn,
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, object]:
    """Preferred channel for the login code: auto (Telegram then e-mail), telegram
    or email."""
    if payload.canal not in ("auto", "telegram", "email"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="canal must be auto, telegram or email")
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    db.execute("UPDATE utilisateur SET mfa_canal = %s WHERE id = %s", (payload.canal, user.id), role=user.role)
    audit.log(user.id, user.role, "mfa_canal", "utilisateur", user.id, {"canal": payload.canal})
    return {"ok": True, "canal": payload.canal}


@router.get("/membres/me/mfa")
def get_mfa_state(
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, object]:
    """Current 2FA state for the member: whether it is on, the channel, the trusted
    devices, and whether to show the (soft) reminder."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    row = db.fetch_one(
        "SELECT mfa_actif, mfa_canal, "
        "(now() - cree_le) > make_interval(months => %s) AS ancien "
        "FROM utilisateur WHERE id = %s",
        (_NUDGE_AFTER_MONTHS, user.id),
        role=user.role,
    ) or {}
    actif = bool(row.get("mfa_actif"))
    appareils = db.fetch_all(
        "SELECT id, libelle, cree_le, derniere_verif_le, expire_le FROM appareil_confiance "
        "WHERE utilisateur_id = %s AND revoque = false AND expire_le > now() "
        "ORDER BY derniere_verif_le DESC",
        (user.id,),
        role=user.role,
    )
    # 2FA is active on the account whenever the universal baseline is enforced OR
    # the member opted into the reinforced mode, so the app can state it honestly
    # rather than presenting an on/off switch that would not really disable it.
    from .config import settings

    verification_active = bool(settings.mfa_baseline_enforced) or actif
    return {
        "actif": actif,  # reinforced mode (shorter trust window) chosen by the member
        "verification_active": verification_active,
        "canal": str(row.get("mfa_canal") or "auto"),
        # Suggest reinforcing only when the member has not opted in; stronger wording
        # after the account has existed for a while. Never a block.
        "recommander": not actif,
        "relance_forte": (not actif) and bool(row.get("ancien")),
        "appareils": [
            {
                "id": str(a["id"]),
                "libelle": a.get("libelle") or "Appareil",
                "cree_le": a["cree_le"].isoformat() if a.get("cree_le") else None,
                "dernier_usage": a["derniere_verif_le"].isoformat() if a.get("derniere_verif_le") else None,
                "expire_le": a["expire_le"].isoformat() if a.get("expire_le") else None,
            }
            for a in appareils
        ],
    }


@router.delete("/membres/me/appareils-confiance/{appareil_id}")
def revoke_appareil(
    appareil_id: str,
    user: Annotated[UserMe, Depends(require_permission("membres.self"))],
) -> dict[str, object]:
    """Revoke a trusted device: the next login from it will require the code again.
    Scoped to the caller's own devices, so a member can only revoke their own."""
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    updated = db.execute(
        "UPDATE appareil_confiance SET revoque = true "
        "WHERE id = %s AND utilisateur_id = %s AND revoque = false RETURNING id",
        (appareil_id, user.id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    audit.log(user.id, user.role, "mfa_appareil_revoque", "appareil_confiance", appareil_id, {})
    return {"ok": True}
