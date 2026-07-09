"""Live presence on a collaboration card: who is viewing it right now.

The client sends a heartbeat while a card is open and polls the viewer list. A viewer
whose last heartbeat is older than the freshness window is dropped. No websocket:
this is a DB-backed heartbeat, consistent with the app's near-real-time polling.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import db
from .collaboration_cartes import _espace_of_carte
from .collaboration_espaces import require_espace_role
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-presence"])

# Everyone who can see the space (including read-only observers) can be present.
VISITEURS = ("proprietaire", "admin", "membre", "observateur")
# A viewer is "live" while their heartbeat is within this many seconds.
_FRAICHEUR_S = 35


class Presence(BaseModel):
    utilisateur_id: str
    nom: str
    initiales: str


def _initiales(nom: str) -> str:
    parts = [p for p in (nom or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@router.post("/cartes-espace/{carte_id}/presence", response_model=list[Presence])
def battement(
    carte_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> list[Presence]:
    _, espace_id = _espace_of_carte(carte_id, user.role)
    require_espace_role(espace_id, user, VISITEURS)
    db.execute(
        "INSERT INTO collab_presence (carte_id, utilisateur_id, maj_le) VALUES (%s, %s, now()) "
        "ON CONFLICT (carte_id, utilisateur_id) DO UPDATE SET maj_le = now()",
        (carte_id, user.id),
        role=user.role,
    )
    rows = db.fetch_all(
        "SELECT u.id, u.email, m.nom_affiche FROM collab_presence p "
        "JOIN utilisateur u ON u.id = p.utilisateur_id LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE p.carte_id = %s AND p.utilisateur_id <> %s "
        f"AND p.maj_le > now() - interval '{_FRAICHEUR_S} seconds'",
        (carte_id, user.id),
        role=user.role,
    )
    out: list[Presence] = []
    for r in rows:
        nom = r["nom_affiche"] or (r["email"].split("@")[0] if r["email"] else "")
        out.append(Presence(utilisateur_id=str(r["id"]), nom=nom, initiales=_initiales(nom)))
    return out


@router.delete("/cartes-espace/{carte_id}/presence", status_code=204)
def quitter(
    carte_id: str,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> None:
    db.execute(
        "DELETE FROM collab_presence WHERE carte_id = %s AND utilisateur_id = %s",
        (carte_id, user.id),
        role=user.role,
    )
