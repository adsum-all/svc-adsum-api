# ruff: noqa: E501
"""Communication diffusion center: a back-office dashboard aggregating the state of
the Informations, their reading, the relay channels and the upcoming scheduled jobs,
with a few guardrails surfaced to the administrator.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from . import db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["communication"])


def _n(row: dict[str, Any] | None, cle: str = "c") -> int:
    return int(row[cle]) if row and row.get(cle) is not None else 0


@router.get("/communication/centre-diffusion")
def centre_diffusion(user: Annotated[UserMe, Depends(require_permission("informations.consulter"))]) -> dict[str, Any]:
    """Aggregate indicators for the Communication > Diffusion center."""
    role = user.role
    actives = db.fetch_one("SELECT count(*) c FROM information WHERE statut = 'envoye' AND (expire_le IS NULL OR expire_le > now())", (), role=role)
    expirant = db.fetch_one("SELECT count(*) c FROM information WHERE statut = 'envoye' AND expire_le BETWEEN now() AND now() + interval '48 hours'", (), role=role)
    brouillons = db.fetch_one("SELECT count(*) c FROM information WHERE statut = 'brouillon'", (), role=role)
    programmees = db.fetch_one("SELECT count(*) c FROM information WHERE statut = 'programme'", (), role=role)
    archivees = db.fetch_one("SELECT count(*) c FROM information WHERE statut = 'archive'", (), role=role)

    # Reading rate over the informations sent in the last 90 days.
    lecture = db.fetch_one(
        "SELECT count(*) tot, count(*) FILTER (WHERE d.statut IN ('lu','confirme')) lus "
        "FROM information_destinataire d JOIN information i ON i.id = d.information_id "
        "WHERE i.statut IN ('envoye','archive') AND i.envoye_le > now() - interval '90 days'",
        (), role=role,
    ) or {}
    tot = int(lecture.get("tot") or 0)
    lus = int(lecture.get("lus") or 0)
    taux_lecture = round(100 * lus / tot) if tot else 0

    # Telegram relays of informations in the last 30 days (contexte information:*).
    tg = db.fetch_one("SELECT count(*) c FROM telegram_message WHERE contexte LIKE %s AND envoye_le > now() - interval '30 days'", ("information:%",), role=role)
    # Delivery failures still open.
    echecs = db.fetch_one("SELECT count(*) c FROM notification_echec WHERE resolu = false", (), role=role) if _table_existe(role, "notification_echec") else {"c": 0}

    # Non-read active informations (a member-facing pressure indicator).
    non_lus = db.fetch_one(
        "SELECT count(*) c FROM information_destinataire d JOIN information i ON i.id = d.information_id "
        "WHERE i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now()) AND d.statut NOT IN ('lu','confirme')",
        (), role=role,
    )

    # Retention last/next run summary.
    retention = db.fetch_one("SELECT execute_le, rapport_ref FROM retention_journal WHERE type_element = 'execution' ORDER BY execute_le DESC LIMIT 1", (), role=role)

    # A few active informations expiring soon, for the operator to review.
    bientot = db.fetch_all(
        "SELECT id, titre, priorite, expire_le FROM information WHERE statut = 'envoye' AND expire_le BETWEEN now() AND now() + interval '48 hours' ORDER BY expire_le ASC LIMIT 10",
        (), role=role,
    )

    return {
        "informations_actives": _n(actives),
        "informations_expirant_48h": _n(expirant),
        "informations_brouillons": _n(brouillons),
        "informations_programmees": _n(programmees),
        "informations_archivees": _n(archivees),
        "taux_lecture": taux_lecture,
        "lus": lus, "destinataires_90j": tot,
        "informations_non_lues": _n(non_lus),
        "telegram_relais_30j": _n(tg),
        "echecs_envoi": _n(echecs),
        "derniere_retention": {
            "execute_le": retention["execute_le"].isoformat() if retention and retention.get("execute_le") else None,
            "rapport": retention.get("rapport_ref") if retention else None,
        } if retention else None,
        "expirant_bientot": [
            {"id": str(r["id"]), "titre": r.get("titre"), "priorite": r.get("priorite"), "expire_le": r["expire_le"].isoformat() if r.get("expire_le") else None}
            for r in (bientot or [])
        ],
    }


def _table_existe(role: str | None, nom: str) -> bool:
    row = db.fetch_one("SELECT 1 AS ok FROM information_schema.tables WHERE table_name = %s", (nom,), role=role)
    return bool(row)
