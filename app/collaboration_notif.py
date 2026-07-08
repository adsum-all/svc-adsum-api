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
    prenom = _prenom(membre_id, role)
    full_ctx = {"prenom": prenom, **(ctx or {})}
    # Positional body params for the (single) approved collaboration WhatsApp
    # template: recipient first name, the subject (card title or requester), the space.
    sujet = str(full_ctx.get("titre") or full_ctx.get("demandeur") or "")
    espace = str(full_ctx.get("espace") or "")
    notifications.notifier(
        membre_id, role, type_offchannel, full_ctx, ref_id=carte_id or "", dedup=dedup,
        whatsapp_params=[prenom, sujet, espace],
    )


def nom_espace(espace_id: str | None, role: str | None) -> str:
    if not espace_id:
        return ""
    row = db.fetch_one("SELECT nom FROM collab_espace WHERE id = %s", (espace_id,), role=role)
    return (row["nom"] if row else "") or ""


def nom_compte(utilisateur_id: str, role: str | None) -> str:
    """Display name of a login account: the linked member's name, else the e-mail
    local part. Used to name the requester in an access-request notification."""
    row = db.fetch_one(
        "SELECT coalesce(m.nom_affiche, u.email) AS nom FROM utilisateur u "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE u.id = %s",
        (utilisateur_id,),
        role=role,
    )
    nom = (row["nom"] if row else "") or "Un collaborateur"
    return nom.split("@")[0] if "@" in nom else nom


def notifier_demande_acces(espace_id: str, espace_nom: str, demandeur_uid: str, role: str | None) -> None:
    """Tell each owner/admin of a space that someone asked to join it, in-app and
    (when they map to a member) over their real channels via the collab_demande
    catalogue message."""
    demandeur = nom_compte(demandeur_uid, role)
    gerants = db.fetch_all(
        "SELECT utilisateur_id FROM collab_espace_membre WHERE espace_id = %s AND role IN ('proprietaire', 'admin')",
        (espace_id,),
        role=role,
    )
    ctx = {"espace": espace_nom, "demandeur": demandeur}
    texte = f"{demandeur} demande a rejoindre l'espace {espace_nom}"
    for g in gerants:
        uid = str(g["utilisateur_id"])
        if uid == demandeur_uid:
            continue
        emettre(uid, "demande_acces", "collab_demande", texte, None, espace_id, ctx, role)
