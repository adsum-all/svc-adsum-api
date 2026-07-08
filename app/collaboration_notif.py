"""Bridge collaboration events to the real multichannel notification engine.

Collaboration identity is the login account (utilisateur); the notification engine
is indexed on the church member (membre) and its channels (email / Telegram /
WhatsApp). This module resolves utilisateur -> membre, writes the in-app
collaboration notification (collab_notification, the dedicated in-app view) and
fans the message out to the member's real channels through notifier(), honouring
the admin toggle, the member preference and the sensitivity matrix. A service
account with no linked member keeps the in-app notification only.
"""
from __future__ import annotations

from typing import Any

from . import db, notifications


def resoudre_membre_id(utilisateur_id: str, role: str | None) -> str | None:
    """The church member linked to a login account, or None for a service account."""
    row = db.fetch_one("SELECT membre_id FROM utilisateur WHERE id = %s", (utilisateur_id,), role=role)
    return str(row["membre_id"]) if row and row["membre_id"] else None


def _prenom(membre_id: str, role: str | None) -> str:
    row = db.fetch_one("SELECT nom_affiche FROM membre WHERE id = %s", (membre_id,), role=role)
    nom = (row["nom_affiche"] if row and row.get("nom_affiche") else "") or ""
    parts = nom.split()
    return parts[0] if parts else "cher membre"


def emettre(
    utilisateur_id: str,
    type_inapp: str,
    type_offchannel: str | None,
    texte: str,
    carte_id: str | None,
    espace_id: str | None,
    ctx: dict[str, Any] | None,
    role: str | None,
    dedup: bool = False,
) -> None:
    """Write the in-app collaboration notification and, when the account maps to a
    member, fan the message out to the real channels via notifier().

    type_inapp is the short in-app kind (mention / assignation / echeance /
    carte_suivie / publication). type_offchannel is the catalogue key
    (collab_mention / collab_assignation / collab_echeance / collab_publication),
    or None to stay in-app only (e.g. high-volume follow notifications).
    """
    db.execute(
        "INSERT INTO collab_notification (utilisateur_id, type, carte_id, espace_id, texte) "
        "VALUES (%s, %s, %s, %s, %s)",
        (utilisateur_id, type_inapp, carte_id, espace_id, texte),
        role=role,
    )
    if not type_offchannel:
        return
    membre_id = resoudre_membre_id(utilisateur_id, role)
    if not membre_id:
        return
    full_ctx = {"prenom": _prenom(membre_id, role), **(ctx or {})}
    notifications.notifier(membre_id, role, type_offchannel, full_ctx, ref_id=carte_id or "", dedup=dedup)


def nom_espace(espace_id: str | None, role: str | None) -> str:
    if not espace_id:
        return ""
    row = db.fetch_one("SELECT nom FROM collab_espace WHERE id = %s", (espace_id,), role=role)
    return (row["nom"] if row else "") or ""
