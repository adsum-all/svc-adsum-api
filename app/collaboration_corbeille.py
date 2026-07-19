# ruff: noqa: E501 - long SQL statements
"""Trash and deferred definitive deletion for workspaces and boards.

Archiving stays as-is; this adds a real deletion path. A workspace (or sub-space)
or a board is sent to the trash (soft-deleted) and stays recoverable for
``retention_suppression_jours`` days (0-90, back-office). A retention of 0 deletes
immediately. The daily job purges expired trash for good. All operations are
guarded by ``collaboration.gerer`` plus the space-level role.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import attachments_core as att
from . import audit, db, storage
from .collaboration_espaces import GERANTS, require_espace_gouvernance, require_espace_role
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-corbeille"])

_MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")


def _sweep_storage(paths: list[str]) -> None:
    """RGPD erasure of bucket objects for rows about to be hard-deleted. Best-effort:
    a storage error never blocks the row deletion (the DB stays consistent)."""
    bucket = att.bucket()
    if not bucket or not paths:
        return
    for p in paths:
        try:
            storage.delete_object(bucket, p)
        except (storage.StorageError, OSError):
            pass


def _paths_tableaux(tableau_ids: list[str], role: str | None) -> list[str]:
    """Storage paths of every card/comment attachment on the given boards."""
    if not tableau_ids:
        return []
    rows = db.fetch_all(
        "SELECT p.storage_path FROM collab_piece p "
        "LEFT JOIN collab_commentaire cm ON cm.id = p.commentaire_id "
        "JOIN collab_carte c ON c.id = coalesce(p.carte_id, cm.carte_id) "
        "WHERE p.storage_path IS NOT NULL AND c.tableau_id = ANY(%s::uuid[])",
        (tableau_ids,), role=role,
    )
    return [str(r["storage_path"]) for r in rows if r.get("storage_path")]


def _paths_canal_espaces(espace_ids: list[str], role: str | None) -> list[str]:
    """Storage paths of every canal voice-note/attachment of the given workspaces."""
    if not espace_ids:
        return []
    rows = db.fetch_all(
        "SELECT p.storage_path FROM collab_piece p "
        "JOIN collab_canal_message m ON m.id = p.canal_message_id "
        "WHERE p.storage_path IS NOT NULL AND m.espace_id = ANY(%s::uuid[])",
        (espace_ids,), role=role,
    )
    return [str(r["storage_path"]) for r in rows if r.get("storage_path")]


class ReglagesIn(BaseModel):
    retention_suppression_jours: int | None = None
    colonne_instructions_auto: bool | None = None
    canal_emetteurs_max: int | None = None
    canaux_additionnels_actives: bool | None = None
    telegram_instruction_bot_token: str | None = None
    telegram_instruction_bot_username: str | None = None


def _int_param(role: str, cle: str, defaut: int, lo: int, hi: int) -> int:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = %s", (cle,), role=role)
    try:
        n = int(str(row["valeur"]).strip('"')) if row and row["valeur"] is not None else defaut
    except (TypeError, ValueError):
        n = defaut
    return max(lo, min(hi, n))


def _bool_param(role: str, cle: str, defaut: bool) -> bool:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = %s", (cle,), role=role)
    if not row or row["valeur"] is None:
        return defaut
    return str(row["valeur"]).strip().lower() not in ("false", '"false"', "0")


def _set_param(role: str, cle: str, valeur_json: str, categorie: str, description: str, uid: str) -> None:
    db.execute(
        "INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le) "
        "VALUES (%s, %s::jsonb, %s, %s, %s, now()) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
        (cle, valeur_json, categorie, description, uid), role=role,
    )


@router.get("/reglages")
def get_reglages(
    user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))],
) -> dict[str, Any]:
    """Collaboration settings the admin can tune: trash retention (0-90 days) and the
    automatic 'Instructions du canal' column on new boards."""
    from . import channels
    return {
        "retention_suppression_jours": retention_jours(user.role),
        "colonne_instructions_auto": _bool_param(user.role, "collab_colonne_instructions_auto", True),
        "canal_emetteurs_max": _int_param(user.role, "canal_emetteurs_max", 1, 1, 50),
        "canaux_additionnels_actives": _bool_param(user.role, "canaux_additionnels_actives", False),
        # Never return the token; only whether a dedicated instruction bot is set.
        "instruction_bot_dedie": channels.telegram_instruction_dedie(),
        "instruction_bot_username": channels.telegram_instruction_username(),
    }


@router.put("/reglages")
def put_reglages(
    payload: ReglagesIn,
    user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))],
) -> dict[str, Any]:
    if payload.retention_suppression_jours is not None:
        n = payload.retention_suppression_jours
        if not 0 <= n <= 90:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="retention doit etre entre 0 et 90 jours")
        _set_param(user.role, "retention_suppression_jours", str(n), "collaboration",
                   "Jours (0-90) de retention avant purge definitive. 0 = immediat.", user.id)
    if payload.colonne_instructions_auto is not None:
        _set_param(user.role, "collab_colonne_instructions_auto", "true" if payload.colonne_instructions_auto else "false",
                   "collaboration", "Colonne Instructions du canal auto sur chaque nouveau tableau.", user.id)
    if payload.canal_emetteurs_max is not None:
        n = payload.canal_emetteurs_max
        if not 1 <= n <= 50:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nombre d'emetteurs entre 1 et 50")
        _set_param(user.role, "canal_emetteurs_max", str(n), "collaboration",
                   "Nombre max d'emetteurs autorises par espace canal.", user.id)
    if payload.canaux_additionnels_actives is not None:
        _set_param(user.role, "canaux_additionnels_actives", "true" if payload.canaux_additionnels_actives else "false",
                   "collaboration", "Active les canaux d'instruction additionnels (desactive par defaut).", user.id)
    # Dedicated instruction bot (distinct from the notification bot): stored in the
    # admin integration table, never returned. Token requires acces.systeme.
    if payload.telegram_instruction_bot_token is not None or payload.telegram_instruction_bot_username is not None:
        if user.role != "super_admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="seule la super-administration configure le bot d'instruction")
        if payload.telegram_instruction_bot_token is not None:
            _set_integration("telegram_instruction_bot_token", payload.telegram_instruction_bot_token.strip(), user.role)
        if payload.telegram_instruction_bot_username is not None:
            _set_integration("telegram_instruction_bot_username", payload.telegram_instruction_bot_username.strip().lstrip("@"), user.role)
    audit.log(user.id, user.role, "config_reglages_collaboration", "parametre", None,
              {"retention": payload.retention_suppression_jours, "colonne_auto": payload.colonne_instructions_auto,
               "emetteurs_max": payload.canal_emetteurs_max, "bot_instruction": payload.telegram_instruction_bot_username})
    return get_reglages(user)


def _set_integration(cle: str, valeur: str, role: str) -> None:
    """Store an admin-managed channel setting (e.g. the instruction bot token)."""
    db.execute(
        "INSERT INTO integration_config (cle, valeur) VALUES (%s, %s) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur",
        (cle, valeur or None), role=role,
    )


def retention_jours(role: str) -> int:
    """Retention window (0-90 days) before a trashed item is purged for good."""
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = 'retention_suppression_jours'", (), role=role)
    try:
        n = int(str(row["valeur"]).strip('"')) if row and row["valeur"] is not None else 14
    except (TypeError, ValueError):
        n = 14
    return max(0, min(90, n))


def _espace_ids_a_purger(espace_id: str, role: str) -> list[str]:
    """The space plus its sub-spaces (a parent takes its children with it)."""
    ids = [espace_id]
    for r in db.fetch_all("SELECT id FROM collab_espace WHERE parent_id = %s", (espace_id,), role=role):
        ids.append(str(r["id"]))
    return ids


def _revoquer_bots(ids: list[str], role: str) -> None:
    """Best-effort revoke the Telegram webhook of each workspace's instruction bot
    before the workspace row (and its token) disappears. Never fatal."""
    from .collaboration_canal_config import _revoquer_webhook, dechiffrer_token
    for r in db.fetch_all(
        "SELECT instruction_bot_token FROM collab_espace "
        "WHERE id = ANY(%s::uuid[]) AND instruction_bot_token IS NOT NULL",
        (ids,), role=role,
    ):
        _revoquer_webhook(dechiffrer_token(r["instruction_bot_token"]))


def _hard_delete_espaces(ids: list[str], role: str, garder_canal: bool = False) -> None:
    """Delete boards then the spaces for good (explicit, no reliance on cascade),
    sweeping their storage objects first (RGPD erasure). The workspace's bot webhook
    is revoked. With ``garder_canal`` the canal instructions are NOT deleted: since
    collab_canal_message.espace_id is ON DELETE SET NULL, they survive as detached
    (admin-only) archives instead of being erased with the workspace."""
    if not ids:
        return
    tableau_ids = [
        str(r["id"]) for r in db.fetch_all(
            "SELECT id FROM collab_tableau WHERE espace_id = ANY(%s::uuid[])", (ids,), role=role
        )
    ]
    paths = _paths_tableaux(tableau_ids, role)
    if not garder_canal:
        paths += _paths_canal_espaces(ids, role)
    _sweep_storage(paths)
    _revoquer_bots(ids, role)
    if not garder_canal:
        # espace_id is SET NULL, so the instructions would otherwise be orphaned; delete
        # them explicitly (their notes/steps/pieces cascade on the message delete).
        db.execute("DELETE FROM collab_canal_message WHERE espace_id = ANY(%s::uuid[])", (ids,), role=role)
    db.execute("DELETE FROM collab_tableau WHERE espace_id = ANY(%s::uuid[])", (ids,), role=role)
    db.execute("DELETE FROM collab_espace WHERE id = ANY(%s::uuid[])", (ids,), role=role)


_POLITIQUES = ("corbeille", "definitif", "definitif_garder_canal", "archiver")


@router.delete("/espaces/{espace_id}")
def supprimer_espace(
    espace_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
    politique: str | None = None,
) -> dict[str, Any]:
    """Remove a workspace (and its sub-spaces) with an explicit policy:

    - ``corbeille`` (default): reversible soft delete for the retention window (or an
      immediate definitive delete when the retention is set to 0).
    - ``definitif`` (A): erase the workspace AND all its canal instructions, boards,
      files and bot link. Irreversible.
    - ``definitif_garder_canal`` (B): erase the workspace but KEEP its canal
      instructions as detached, admin-only archives.
    - ``archiver`` (C): archive the workspace (and sub-spaces) instead of deleting; fully
      reversible. (D, disabling only the bot, is the separate bot-dissociation action.)
    """
    eid = str(espace_id)
    require_espace_gouvernance(eid, user, GERANTS)
    ids = _espace_ids_a_purger(eid, user.role)
    pol = (politique or "corbeille").strip()
    if pol not in _POLITIQUES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"politique invalide (attendu: {', '.join(_POLITIQUES)})")

    if pol == "archiver":
        db.execute("UPDATE collab_espace SET archive = true, maj_le = now() WHERE id = ANY(%s::uuid[])", (ids,), role=user.role)
        audit.log(user.id, user.role, "collab_espace_archive", "collab_espace", eid, {"sous_espaces": len(ids) - 1})
        return {"archive": True, "politique": pol}

    if pol in ("definitif", "definitif_garder_canal"):
        garder = pol == "definitif_garder_canal"
        _hard_delete_espaces(ids, user.role, garder_canal=garder)
        audit.log(user.id, user.role, "collab_espace_suppression_definitive", "collab_espace", eid,
                  {"politique": pol, "canal_conserve": garder})
        return {"supprime": True, "definitif": True, "canal_conserve": garder, "politique": pol}

    # Default: corbeille (or immediate if retention is 0).
    jours = retention_jours(user.role)
    if jours == 0:
        _hard_delete_espaces(ids, user.role)
        audit.log(user.id, user.role, "collab_espace_suppression_definitive", "collab_espace", eid, {"immediat": True})
        return {"supprime": True, "definitif": True, "retention_jours": 0, "politique": "definitif"}
    db.execute(
        "UPDATE collab_espace SET supprime_le = now(), supprime_par = %s WHERE id = ANY(%s::uuid[])",
        (user.id, ids), role=user.role,
    )
    audit.log(user.id, user.role, "collab_espace_corbeille", "collab_espace", eid, {"retention_jours": jours})
    return {"supprime": True, "definitif": False, "retention_jours": jours, "politique": "corbeille"}


@router.post("/espaces/{espace_id}/restaurer")
def restaurer_espace(
    espace_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, Any]:
    """Restore a trashed workspace (and its sub-spaces) before it is purged."""
    eid = str(espace_id)
    # Same governance gate as the deletion that put it here: a collaboration.gerer
    # holder must actually govern THIS space (owner/admin, or a platform admin), never
    # restore an arbitrary space by id. Symmetric with supprimer_espace.
    require_espace_gouvernance(eid, user, GERANTS)
    ids = _espace_ids_a_purger(eid, user.role)
    row = db.execute(
        "UPDATE collab_espace SET supprime_le = NULL, supprime_par = NULL WHERE id = ANY(%s::uuid[]) AND supprime_le IS NOT NULL RETURNING id",
        (ids,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="espace absent de la corbeille")
    audit.log(user.id, user.role, "collab_espace_restauration", "collab_espace", eid, {})
    return {"restaure": True}


@router.delete("/tableaux-espace/{tableau_id}")
def supprimer_tableau(
    tableau_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, Any]:
    """Send a board to the trash, or delete it immediately when retention is 0."""
    tid = str(tableau_id)
    row = db.fetch_one("SELECT espace_id FROM collab_tableau WHERE id = %s", (tid,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tableau introuvable")
    require_espace_role(str(row["espace_id"]), user, _MEMBRES_ACTIFS)
    jours = retention_jours(user.role)
    if jours == 0:
        _sweep_storage(_paths_tableaux([tid], user.role))
        db.execute("DELETE FROM collab_tableau WHERE id = %s", (tid,), role=user.role)
        audit.log(user.id, user.role, "collab_tableau_suppression_definitive", "collab_tableau", tid, {"immediat": True})
        return {"supprime": True, "definitif": True, "retention_jours": 0}
    db.execute("UPDATE collab_tableau SET supprime_le = now(), supprime_par = %s WHERE id = %s", (user.id, tid), role=user.role)
    audit.log(user.id, user.role, "collab_tableau_corbeille", "collab_tableau", tid, {"retention_jours": jours})
    return {"supprime": True, "definitif": False, "retention_jours": jours}


@router.post("/tableaux-espace/{tableau_id}/restaurer")
def restaurer_tableau(
    tableau_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, Any]:
    tid = str(tableau_id)
    # Same space-role gate as the deletion (require_espace_role MEMBRES_ACTIFS): the board
    # is soft-deleted but its espace_id row still exists, so resolve it and check the caller
    # belongs to that space before restoring, never restore another space's board by id.
    src = db.fetch_one("SELECT espace_id FROM collab_tableau WHERE id = %s AND supprime_le IS NOT NULL", (tid,), role=user.role)
    if not src:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tableau absent de la corbeille")
    require_espace_role(str(src["espace_id"]), user, _MEMBRES_ACTIFS)
    row = db.execute(
        "UPDATE collab_tableau SET supprime_le = NULL, supprime_par = NULL WHERE id = %s AND supprime_le IS NOT NULL RETURNING id",
        (tid,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tableau absent de la corbeille")
    audit.log(user.id, user.role, "collab_tableau_restauration", "collab_tableau", tid, {})
    return {"restaure": True}


@router.get("/corbeille")
def corbeille(
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> dict[str, Any]:
    """Trashed workspaces and boards the caller belongs to, with the days left before
    definitive purge. Membership-scoped (a technical super-admin sees everything): a
    collaborator must never enumerate the trashed spaces/boards of workspaces it never
    joined, the same cross-space isolation enforced across this module."""
    jours = retention_jours(user.role)
    if user.acces_technique_global:
        espaces = db.fetch_all(
            "SELECT id, nom, parent_id, supprime_le, "
            "greatest(0, %s - floor(extract(epoch FROM (now() - supprime_le)) / 86400)::int) AS jours_restants "
            "FROM collab_espace WHERE supprime_le IS NOT NULL ORDER BY supprime_le DESC",
            (jours,), role=user.role,
        )
        tableaux = db.fetch_all(
            "SELECT id, nom, espace_id, supprime_le, "
            "greatest(0, %s - floor(extract(epoch FROM (now() - supprime_le)) / 86400)::int) AS jours_restants "
            "FROM collab_tableau WHERE supprime_le IS NOT NULL ORDER BY supprime_le DESC",
            (jours,), role=user.role,
        )
    else:
        espaces = db.fetch_all(
            "SELECT e.id, e.nom, e.parent_id, e.supprime_le, "
            "greatest(0, %s - floor(extract(epoch FROM (now() - e.supprime_le)) / 86400)::int) AS jours_restants "
            "FROM collab_espace e WHERE e.supprime_le IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM collab_espace_membre m WHERE m.espace_id = e.id AND m.utilisateur_id = %s) "
            "ORDER BY e.supprime_le DESC",
            (jours, user.id), role=user.role,
        )
        tableaux = db.fetch_all(
            "SELECT t.id, t.nom, t.espace_id, t.supprime_le, "
            "greatest(0, %s - floor(extract(epoch FROM (now() - t.supprime_le)) / 86400)::int) AS jours_restants "
            "FROM collab_tableau t WHERE t.supprime_le IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM collab_espace_membre m WHERE m.espace_id = t.espace_id AND m.utilisateur_id = %s) "
            "ORDER BY t.supprime_le DESC",
            (jours, user.id), role=user.role,
        )
    return {
        "retention_jours": jours,
        "espaces": [
            {"id": str(e["id"]), "nom": e["nom"], "sous_espace": bool(e["parent_id"]),
             "supprime_le": e["supprime_le"].isoformat() if e["supprime_le"] else None,
             "jours_restants": int(e["jours_restants"])}
            for e in espaces
        ],
        "tableaux": [
            {"id": str(t["id"]), "nom": t["nom"], "espace_id": str(t["espace_id"]),
             "supprime_le": t["supprime_le"].isoformat() if t["supprime_le"] else None,
             "jours_restants": int(t["jours_restants"])}
            for t in tableaux
        ],
    }


def purger_suppressions(role: str | None) -> dict[str, int]:
    """Hard-delete trashed items past the retention window. Idempotent, safe to run
    on every daily pass."""
    jours = retention_jours(role or "super_admin")
    seuil = f"now() - interval '{jours} days'"
    espaces = db.fetch_all(
        f"SELECT id FROM collab_espace WHERE supprime_le IS NOT NULL AND supprime_le < {seuil}", (), role=role,
    )
    eids = [str(r["id"]) for r in espaces]
    if eids:
        _hard_delete_espaces(eids, role)
    # Boards trashed on their own (not via a trashed space): sweep their storage
    # objects before the rows disappear, then hard-delete them.
    tab_expires = [
        str(r["id"]) for r in db.fetch_all(
            f"SELECT id FROM collab_tableau WHERE supprime_le IS NOT NULL AND supprime_le < {seuil}", (), role=role,
        )
    ]
    if tab_expires:
        _sweep_storage(_paths_tableaux(tab_expires, role))
        db.execute("DELETE FROM collab_tableau WHERE id = ANY(%s::uuid[])", (tab_expires,), role=role)
    return {"espaces": len(eids), "tableaux": len(tab_expires)}
