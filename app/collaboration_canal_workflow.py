# ruff: noqa: E501 - long SQL statements
"""Instruction workflow on the moderator channel.

A channel subject is an instruction with a lifecycle: taken in charge, assigned to
a workspace and people, loaded onto a board as a card, processed, restituted
(report + short summary) and closed. Each transition is stamped in
collab_canal_etape (who, when) so a Jira-style workflow view can be rendered, and a
readable reference (INS-YYYY-NNNNNN) is minted on first take-in-charge.

A closed instruction cannot be reopened or reassigned; only complements (channel
replies) may still be added. Every write here is guarded by ``canal.traiter`` (the
channel's processing permission), plus per-space membership isolation.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from . import collaboration_canal as cc
from .fields import LongTextStr, ShortStr, TextStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-canal-workflow"])


class AffecterIn(BaseModel):
    espace_id: ShortStr | None = None
    assignes: Annotated[list[ShortStr], Field(max_length=50)] = []


class ChargerTableauIn(BaseModel):
    tableau_id: ShortStr
    colonne_id: ShortStr


class RestituerIn(BaseModel):
    rapport: LongTextStr = ""
    resume: TextStr = ""


def _msg(mid: str, user: UserMe) -> dict[str, Any]:
    # Isolation first: a non-admin can only act on an instruction of a workspace
    # they belong to (403 otherwise). Then load the workflow fields.
    cc._garde_message(mid, user)
    row = db.fetch_one("SELECT id, etape, matricule, cloture_le FROM collab_canal_message WHERE id = %s", (mid,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instruction introuvable")
    return row


def _refuser_si_cloture(row: dict[str, Any]) -> None:
    if row.get("cloture_le") or row.get("etape") == "cloturee":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="instruction cloturee: aucune reprise possible (seuls des complements peuvent etre ajoutes)",
        )


def _matricule(mid: str, role: str, current: object) -> str:
    """Mint INS-YYYY-NNNNNN on first take-in-charge; keep an existing one."""
    if current:
        return str(current)
    row = db.execute(
        "UPDATE collab_canal_message SET matricule = "
        "'INS-' || to_char(now(), 'YYYY') || '-' || lpad(nextval('seq_instruction')::text, 6, '0') "
        "WHERE id = %s AND matricule IS NULL RETURNING matricule",
        (mid,), role=role,
    )
    if row:
        return str(row["matricule"])
    got = db.fetch_one("SELECT matricule FROM collab_canal_message WHERE id = %s", (mid,), role=role)
    return str((got or {}).get("matricule") or "")


def _etape(mid: str, etape: str, acteur: str, detail: str, role: str) -> None:
    db.execute(
        "INSERT INTO collab_canal_etape (message_id, etape, acteur_id, detail) VALUES (%s, %s, %s, %s)",
        (mid, etape, acteur, detail or None), role=role,
    )


def _nom(uid: str, role: str) -> str:
    row = db.fetch_one(
        "SELECT COALESCE(m.nom_affiche, u.email) AS nom FROM utilisateur u "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE u.id = %s", (uid,), role=role,
    )
    return str((row or {}).get("nom") or "un responsable")


def _notifier_emetteur(mid: str, texte: str, role: str) -> None:
    """Send a concise Telegram update to the instruction's emitter (the member who
    left it), if their Telegram is linked. Never breaks the workflow."""
    try:
        row = db.fetch_one(
            "SELECT m.id AS membre_id, m.telegram_chat_id AS chat FROM collab_canal_message cm "
            "JOIN utilisateur u ON u.id = cm.auteur_id JOIN membre m ON m.id = u.membre_id "
            "WHERE cm.id = %s", (mid,), role=role,
        )
        chat = row and row.get("chat")
        if not chat:
            return
        # Honor the member's Telegram preference: a member who turned Telegram off does not
        # get this update (the On/Off switch applies to every channel path, uniformly).
        pref = db.fetch_one("SELECT telegram FROM preference_notification WHERE membre_id = %s", (row["membre_id"],), role=role)
        if pref is not None and not pref.get("telegram"):
            return
        from .channels import Message, send_telegram
        send_telegram(str(chat), Message(titre="ADSUM Canal", corps_text=texte), contexte=f"instruction:{mid}")
    except Exception:  # noqa: BLE001 - a notification failure must never break the workflow
        pass


@router.post("/canal/{message_id}/prise-en-charge", response_model=cc.CanalMessageOut)
def prise_en_charge(
    message_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """A canal manager takes the instruction in charge: mint its reference, record
    who/when, move it to the 'prise_en_charge' stage."""
    mid = str(message_id)
    row = _msg(mid, user)
    _refuser_si_cloture(row)
    mat = _matricule(mid, user.role, row.get("matricule"))
    db.execute(
        "UPDATE collab_canal_message SET prise_en_charge_par = %s, prise_en_charge_le = now(), "
        "etape = 'prise_en_charge', statut = 'en_cours', maj_le = now() WHERE id = %s",
        (user.id, mid), role=user.role,
    )
    _etape(mid, "prise_en_charge", user.id, "", user.role)
    _notifier_emetteur(mid, f"{mat} : pris en charge par {_nom(user.id, user.role)}.", user.role)
    audit.log(user.id, user.role, "canal_prise_en_charge", "collab_canal_message", mid, {"matricule": mat})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/{message_id}/affecter", response_model=cc.CanalMessageOut)
def affecter(
    message_id: uuid.UUID,
    payload: AffecterIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Assign the instruction to a workspace (sub-space/direction) and to one or
    several people, and move it to the 'affectee' stage."""
    mid = str(message_id)
    row = _msg(mid, user)
    _refuser_si_cloture(row)
    espace_id = payload.espace_id or None
    if espace_id and not db.fetch_one("SELECT 1 FROM collab_espace WHERE id = %s", (espace_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="espace cible introuvable")
    assignes = [a for a in dict.fromkeys(payload.assignes) if a]
    if assignes:
        valides = {
            str(u["id"]) for u in db.fetch_all(
                "SELECT id FROM utilisateur WHERE id = ANY(%s::uuid[]) AND actif = true", (assignes,), role=user.role,
            )
        }
        inconnus = [a for a in assignes if a not in valides]
        if inconnus:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="assigne(s) invalide(s)")
    db.execute("DELETE FROM collab_canal_assignation WHERE message_id = %s", (mid,), role=user.role)
    for a in assignes:
        db.execute(
            "INSERT INTO collab_canal_assignation (message_id, utilisateur_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (mid, a), role=user.role,
        )
    primaire = assignes[0] if assignes else None
    db.execute(
        "UPDATE collab_canal_message SET assigne_espace_id = %s, assigne_id = %s, etape = 'affectee', maj_le = now() WHERE id = %s",
        (espace_id, primaire, mid), role=user.role,
    )
    _etape(mid, "affectee", user.id, f"espace={espace_id or '-'} assignes={len(assignes)}", user.role)
    _mat = str(row.get("matricule") or "")
    _en = ""
    if espace_id:
        _e = db.fetch_one("SELECT nom FROM collab_espace WHERE id = %s", (espace_id,), role=user.role)
        _en = f" a {(_e or {}).get('nom')}" if _e else ""
    _notifier_emetteur(mid, f"{_mat} : affecte{_en}, {len(assignes)} personne(s) assignee(s).", user.role)
    audit.log(user.id, user.role, "canal_affectation", "collab_canal_message", mid,
              {"espace_id": espace_id, "assignes": assignes})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/{message_id}/charger-tableau", response_model=cc.CanalMessageOut)
def charger_tableau(
    message_id: uuid.UUID,
    payload: ChargerTableauIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Load the instruction onto a board as a card (in the chosen column) and move
    it to 'en_traitement'. Reuses the card creation, so the source link is kept."""
    mid = str(message_id)
    row = _msg(mid, user)
    _refuser_si_cloture(row)
    cc.creer_carte_depuis_canal(message_id, cc.CreerCarteIn(tableau_id=payload.tableau_id, colonne_id=payload.colonne_id), user)
    db.execute(
        "UPDATE collab_canal_message SET etape = 'en_traitement', maj_le = now() WHERE id = %s", (mid,), role=user.role,
    )
    _etape(mid, "en_traitement", user.id, f"tableau={payload.tableau_id}", user.role)
    _notifier_emetteur(mid, f"{row.get('matricule') or ''} : en cours de traitement.", user.role)
    audit.log(user.id, user.role, "canal_charge_tableau", "collab_canal_message", mid, {"tableau_id": payload.tableau_id})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/{message_id}/restituer", response_model=cc.CanalMessageOut)
def restituer(
    message_id: uuid.UUID,
    payload: RestituerIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Record the restitution (detailed report + short summary) and move the
    instruction to 'restituee' (awaiting verification/closure)."""
    mid = str(message_id)
    row = _msg(mid, user)
    _refuser_si_cloture(row)
    db.execute(
        "UPDATE collab_canal_message SET restitution_rapport = %s, restitution_resume = %s, "
        "etape = 'restituee', maj_le = now() WHERE id = %s",
        (payload.rapport or None, payload.resume or None, mid), role=user.role,
    )
    _etape(mid, "restituee", user.id, "", user.role)
    audit.log(user.id, user.role, "canal_restitution", "collab_canal_message", mid, {"resume": bool(payload.resume)})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/{message_id}/cloturer", response_model=cc.CanalMessageOut)
def cloturer(
    message_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Close the instruction for good: it can no longer be reopened or reassigned,
    only complemented."""
    mid = str(message_id)
    row = _msg(mid, user)
    if row.get("cloture_le"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="instruction deja cloturee")
    db.execute(
        "UPDATE collab_canal_message SET cloture_le = now(), cloture_par = %s, etape = 'cloturee', "
        "statut = 'traite', maj_le = now() WHERE id = %s",
        (user.id, mid), role=user.role,
    )
    _etape(mid, "cloturee", user.id, "", user.role)
    _fin = db.fetch_one("SELECT matricule, restitution_resume FROM collab_canal_message WHERE id = %s", (mid,), role=user.role) or {}
    _resume = (_fin.get("restitution_resume") or "").strip()
    _notifier_emetteur(mid, f"{_fin.get('matricule') or ''} : demande traitee." + (f" {_resume}" if _resume else ""), user.role)
    audit.log(user.id, user.role, "canal_cloture", "collab_canal_message", mid, {})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.get("/canal/{message_id}/workflow")
def workflow(
    message_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.voir"))],
) -> list[dict[str, object]]:
    """The step-by-step history of the instruction (for the workflow view)."""
    mid = str(message_id)
    cc._garde_message(mid, user)
    rows = db.fetch_all(
        "SELECT e.etape, e.detail, e.cree_le, COALESCE(m.nom_affiche, u.email) AS acteur "
        "FROM collab_canal_etape e LEFT JOIN utilisateur u ON u.id = e.acteur_id "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE e.message_id = %s ORDER BY e.cree_le",
        (mid,), role=user.role,
    )
    return [
        {"etape": r["etape"], "detail": r["detail"], "acteur": r["acteur"],
         "cree_le": r["cree_le"].isoformat() if r["cree_le"] else None}
        for r in rows
    ]
