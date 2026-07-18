# ruff: noqa: E501
"""Retention and archiving engine for communications, plus its admin surface.

Two-step lifecycle, never a silent destruction:

1. ARCHIVE: an Information older than the archive window (default 24 months) that
   is NOT protected, NOT institutional and NOT pinned moves to ``statut='archive'``
   (still auditable, restorable). Read notifications older than their window are
   archived; unread ones after a longer window. Security notifications are never
   touched.
2. DELETE: only when the administrator explicitly turned ``retention_auto_suppression``
   ON, and only after the deletion window. OFF by default, so nothing is ever
   deleted without an opt-in.

Every run appends rows to ``retention_journal`` (audit). A simulation counts what
WOULD happen without mutating anything. Telegram cleanup is delegated to
``channels.purge_old_telegram`` and only attempts what the Bot API actually allows.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from . import channels, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["retention"])

# Notification types that must survive retention (security / audit trail).
_NOTIF_PROTEGES = (
    "securite_alerte", "connexion_inhabituelle", "compte_bloque", "compte_debloque",
    "otp", "otp_expire", "engagement_code", "compte_cree",
)

_CFG_DEFAUTS = {
    "retention_info_archive_mois": 24,
    "retention_info_suppression_mois": 0,
    "retention_notif_lues_jours": 90,
    "retention_notif_nonlues_jours": 180,
    "retention_notif_suppression_mois": 24,
    "telegram_retention_jours": 14,
}


def _cfg_int(cle: str, defaut: int) -> int:
    raw = channels.integration_value(cle)
    try:
        return int(raw) if raw not in (None, "") else defaut
    except (TypeError, ValueError):
        return defaut


def _cfg_bool(cle: str, defaut: bool) -> bool:
    raw = channels.integration_value(cle)
    if raw is None:
        return defaut
    return str(raw).strip().lower() in ("true", "1", "oui", "yes", "on")


def _journal(role: str | None, **champs: Any) -> None:
    db.execute(
        """
        INSERT INTO retention_journal
          (type_element, element_id, titre, element_cree_le, archive_le, supprime_le, regle, acteur, destinataires, a_media, resultat, motif_exclusion, rapport_ref)
        VALUES (%(type_element)s, %(element_id)s, %(titre)s, %(element_cree_le)s, %(archive_le)s, %(supprime_le)s, %(regle)s, %(acteur)s, %(destinataires)s, %(a_media)s, %(resultat)s, %(motif_exclusion)s, %(rapport_ref)s)
        """,
        {
            "type_element": champs.get("type_element"), "element_id": champs.get("element_id"), "titre": champs.get("titre"),
            "element_cree_le": champs.get("element_cree_le"), "archive_le": champs.get("archive_le"), "supprime_le": champs.get("supprime_le"),
            "regle": champs.get("regle"), "acteur": champs.get("acteur"), "destinataires": champs.get("destinataires"),
            "a_media": champs.get("a_media"), "resultat": champs.get("resultat"), "motif_exclusion": champs.get("motif_exclusion"),
            "rapport_ref": champs.get("rapport_ref"),
        },
        role=role,
    )


def executer_retention(simulation: bool, acteur: str, role: str | None) -> dict[str, Any]:
    """Run (or simulate) the retention policy. Returns a report and writes the audit."""
    archive_mois = _cfg_int("retention_info_archive_mois", 24)
    suppr_info_mois = _cfg_int("retention_info_suppression_mois", 0)
    lues_jours = _cfg_int("retention_notif_lues_jours", 90)
    nonlues_jours = _cfg_int("retention_notif_nonlues_jours", 180)
    suppr_notif_mois = _cfg_int("retention_notif_suppression_mois", 24)
    auto_suppr = _cfg_bool("retention_auto_suppression", False)

    rapport: dict[str, Any] = {
        "simulation": simulation, "acteur": acteur,
        "informations_archivees": 0, "informations_protegees": 0, "informations_supprimees": 0,
        "notifications_archivees": 0, "notifications_supprimees": 0, "telegram_traites": 0,
        "suppression_active": auto_suppr,
    }

    # 1. Informations to archive (per-item audit; institutional broadcasts are few).
    a_archiver = db.fetch_all(
        """
        SELECT id, titre, cree_le, (audio_url IS NOT NULL OR document_url IS NOT NULL OR image_url IS NOT NULL) AS a_media
        FROM information
        WHERE statut = 'envoye'
          AND envoye_le < now() - make_interval(months => %s)
          AND NOT protege AND NOT institutionnelle
          AND (epingle_jusqu IS NULL OR epingle_jusqu < now())
        ORDER BY envoye_le ASC LIMIT 500
        """,
        (archive_mois,), role=role,
    )
    for info in a_archiver or []:
        if not simulation:
            db.execute("UPDATE information SET statut = 'archive' WHERE id = %s", (str(info["id"]),), role=role)
            _journal(role, type_element="information", element_id=str(info["id"]), titre=info.get("titre"),
                     element_cree_le=info.get("cree_le"), regle=f"archive_apres_{archive_mois}_mois",
                     acteur=acteur, a_media=bool(info.get("a_media")), resultat="archive")
        rapport["informations_archivees"] += 1

    # Count protected/institutional ones that would otherwise be eligible (excluded).
    proteges = db.fetch_one(
        """
        SELECT count(*) AS c FROM information
        WHERE statut = 'envoye' AND envoye_le < now() - make_interval(months => %s)
          AND (protege OR institutionnelle)
        """,
        (archive_mois,), role=role,
    )
    rapport["informations_protegees"] = int(proteges["c"]) if proteges else 0

    # 2. Delete archived informations, ONLY when the admin opted in.
    if auto_suppr and suppr_info_mois > 0:
        a_suppr = db.fetch_all(
            """
            SELECT id, titre, cree_le FROM information
            WHERE statut = 'archive' AND NOT protege AND NOT institutionnelle
              AND maj_le < now() - make_interval(months => %s)
            ORDER BY maj_le ASC LIMIT 200
            """,
            (suppr_info_mois,), role=role,
        )
        for info in a_suppr or []:
            if not simulation:
                db.execute("DELETE FROM information_destinataire WHERE information_id = %s", (str(info["id"]),), role=role)
                db.execute("DELETE FROM information WHERE id = %s", (str(info["id"]),), role=role)
                _journal(role, type_element="information", element_id=str(info["id"]), titre=info.get("titre"),
                         element_cree_le=info.get("cree_le"), regle=f"suppression_apres_{suppr_info_mois}_mois_archivage",
                         acteur=acteur, resultat="supprime")
            rapport["informations_supprimees"] += 1

    # 3. Notifications: archive read then unread (security types excluded). Summary audit.
    if simulation:
        row = db.fetch_one(
            "SELECT count(*) AS c FROM notification WHERE NOT archive AND ((lu AND coalesce(lu_le, cree_le) < now() - make_interval(days => %s)) OR (NOT lu AND cree_le < now() - make_interval(days => %s))) AND type <> ALL(%s)",
            (lues_jours, nonlues_jours, list(_NOTIF_PROTEGES)), role=role,
        )
        rapport["notifications_archivees"] = int(row["c"]) if row else 0
    else:
        # Count the exact set first (deterministic), then archive it in one statement.
        avant = db.fetch_one(
            "SELECT count(*) AS c FROM notification WHERE NOT archive AND ((lu AND coalesce(lu_le, cree_le) < now() - make_interval(days => %s)) OR (NOT lu AND cree_le < now() - make_interval(days => %s))) AND type <> ALL(%s)",
            (lues_jours, nonlues_jours, list(_NOTIF_PROTEGES)), role=role,
        )
        n = int(avant["c"]) if avant else 0
        if n:
            db.execute(
                "UPDATE notification SET archive = true, archive_le = now() WHERE NOT archive AND ((lu AND coalesce(lu_le, cree_le) < now() - make_interval(days => %s)) OR (NOT lu AND cree_le < now() - make_interval(days => %s))) AND type <> ALL(%s)",
                (lues_jours, nonlues_jours, list(_NOTIF_PROTEGES)), role=role,
            )
            _journal(role, type_element="notification", regle=f"archive_lues_{lues_jours}j_nonlues_{nonlues_jours}j",
                     acteur=acteur, destinataires=n, resultat="archive")
        rapport["notifications_archivees"] = n

    # 4. Delete archived notifications, ONLY when opted in.
    if auto_suppr and suppr_notif_mois > 0:
        if simulation:
            row = db.fetch_one(
                "SELECT count(*) AS c FROM notification WHERE archive AND archive_le < now() - make_interval(months => %s) AND type <> ALL(%s)",
                (suppr_notif_mois, list(_NOTIF_PROTEGES)), role=role,
            )
            rapport["notifications_supprimees"] = int(row["c"]) if row else 0
        else:
            avant = db.fetch_one(
                "SELECT count(*) AS c FROM notification WHERE archive AND archive_le < now() - make_interval(months => %s) AND type <> ALL(%s)",
                (suppr_notif_mois, list(_NOTIF_PROTEGES)), role=role,
            )
            n = int(avant["c"]) if avant else 0
            if n:
                db.execute(
                    "DELETE FROM notification WHERE archive AND archive_le < now() - make_interval(months => %s) AND type <> ALL(%s)",
                    (suppr_notif_mois, list(_NOTIF_PROTEGES)), role=role,
                )
                _journal(role, type_element="notification", regle=f"suppression_apres_{suppr_notif_mois}_mois_archivage",
                         acteur=acteur, destinataires=n, resultat="supprime")
            rapport["notifications_supprimees"] = n

    # 5. Telegram: only messages ADSUM sent, only within the Bot API delete window.
    if not simulation:
        try:
            rapport["telegram_traites"] = channels.purge_old_telegram(role)
        except Exception:  # noqa: BLE001 - Telegram cleanup never breaks retention
            rapport["telegram_traites"] = 0
    else:
        tg_jours = _cfg_int("telegram_retention_jours", 14)
        row = db.fetch_one(
            "SELECT count(*) AS c FROM telegram_message WHERE supprime_le IS NULL AND envoye_le < now() - make_interval(days => %s)",
            (tg_jours,), role=role,
        )
        rapport["telegram_traites"] = int(row["c"]) if row else 0

    # Run summary row.
    _journal(role, type_element="execution", regle="retention_communications", acteur=acteur,
             resultat="simulation" if simulation else "execute",
             rapport_ref=f"info_arch={rapport['informations_archivees']} info_suppr={rapport['informations_supprimees']} notif_arch={rapport['notifications_archivees']} notif_suppr={rapport['notifications_supprimees']} tg={rapport['telegram_traites']}")
    return rapport


# --- Admin surface ----------------------------------------------------------

class RetentionConfig(BaseModel):
    retention_info_archive_mois: int
    retention_info_suppression_mois: int
    retention_notif_lues_jours: int
    retention_notif_nonlues_jours: int
    retention_notif_suppression_mois: int
    retention_auto_suppression: bool
    telegram_retention_jours: int


@router.get("/parametres/retention")
def lire_config(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, Any]:
    """Current retention settings, plus the REAL Telegram deletion capability and
    the last run, so the administrator sees the requested rule and what is actually
    technically applicable."""
    cfg = {k: _cfg_int(k, d) for k, d in _CFG_DEFAUTS.items()}
    cfg["retention_auto_suppression"] = _cfg_bool("retention_auto_suppression", False)
    dernier = db.fetch_one(
        "SELECT execute_le, rapport_ref, resultat FROM retention_journal WHERE type_element = 'execution' ORDER BY execute_le DESC LIMIT 1",
        (), role=user.role,
    )
    return {
        "config": cfg,
        "telegram_capacite": _telegram_capacite(user.role),
        "derniere_execution": {
            "execute_le": dernier["execute_le"].isoformat() if dernier and dernier.get("execute_le") else None,
            "rapport": dernier.get("rapport_ref") if dernier else None,
            "resultat": dernier.get("resultat") if dernier else None,
        } if dernier else None,
    }


def _telegram_capacite(role: str | None) -> dict[str, Any]:
    """Honest statement of what the bot can actually delete. The Telegram Bot API
    only lets a bot delete its OWN messages, and in practice reliably only within a
    limited window (about 48 hours). The 14-day setting is an ADSUM display window,
    not a deletion guarantee."""
    tg_jours = _cfg_int("telegram_retention_jours", 14)
    eligibles = db.fetch_one(
        "SELECT count(*) AS c FROM telegram_message WHERE supprime_le IS NULL AND envoye_le < now() - make_interval(days => %s)",
        (tg_jours,), role=role,
    )
    return {
        "retention_configuree_jours": tg_jours,
        "fenetre_suppression_bot_heures": 48,
        "messages_eligibles": int(eligibles["c"]) if eligibles else 0,
        "note": "Telegram ne garantit la suppression par le bot que dans une fenêtre technique limitée (environ 48 heures) et uniquement pour les messages envoyés par ADSUM. Au-delà, les messages restent dans Telegram; l'information complète reste disponible dans ADSUM.",
    }


@router.put("/parametres/retention", status_code=204)
def maj_config(payload: RetentionConfig, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    """Update the retention settings. Bounded so a misconfiguration cannot wipe data."""
    valeurs = {
        "retention_info_archive_mois": max(1, min(240, payload.retention_info_archive_mois)),
        "retention_info_suppression_mois": max(0, min(240, payload.retention_info_suppression_mois)),
        "retention_notif_lues_jours": max(1, min(3650, payload.retention_notif_lues_jours)),
        "retention_notif_nonlues_jours": max(1, min(3650, payload.retention_notif_nonlues_jours)),
        "retention_notif_suppression_mois": max(0, min(240, payload.retention_notif_suppression_mois)),
        "telegram_retention_jours": max(1, min(365, payload.telegram_retention_jours)),
        "retention_auto_suppression": "true" if payload.retention_auto_suppression else "false",
    }
    for cle, valeur in valeurs.items():
        db.execute(
            "INSERT INTO integration_config (cle, valeur, maj_par, maj_le) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
            (cle, str(valeur), user.id), role=user.role,
        )


@router.post("/retention/executer")
def executer(user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))], simulation: bool = Query(default=True)) -> dict[str, Any]:
    """Run the retention policy now (simulation by default). A real run archives and,
    only if auto-deletion is ON, deletes; every action is journaled."""
    return executer_retention(simulation=simulation, acteur=f"admin:{user.id}", role=user.role)


@router.get("/retention/journal")
def journal(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))], limit: int = Query(default=30, ge=1, le=200), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """The retention audit trail (archive, delete, protected, run summaries)."""
    total_row = db.fetch_one("SELECT count(*) AS c FROM retention_journal", (), role=user.role)
    rows = db.fetch_all(
        "SELECT type_element, element_id, titre, regle, acteur, destinataires, resultat, motif_exclusion, rapport_ref, execute_le FROM retention_journal ORDER BY execute_le DESC LIMIT %s OFFSET %s",
        (limit, offset), role=user.role,
    )
    items = [
        {
            "type_element": r.get("type_element"), "element_id": str(r["element_id"]) if r.get("element_id") else None,
            "titre": r.get("titre"), "regle": r.get("regle"), "acteur": r.get("acteur"),
            "destinataires": r.get("destinataires"), "resultat": r.get("resultat"),
            "motif_exclusion": r.get("motif_exclusion"), "rapport": r.get("rapport_ref"),
            "execute_le": r["execute_le"].isoformat() if r.get("execute_le") else None,
        }
        for r in rows
    ]
    return {"items": items, "total": int(total_row["c"]) if total_row else 0}
