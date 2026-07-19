# ruff: noqa: E501
"""Admin actions on a sent Information: delivery CSV export and re-send to unread.

Kept apart from ``information.py`` for size. These are back-office operations for
tracking and following up an institutional communication:

- CSV export of the per-recipient delivery status (sent, read, confirmed), for a
  report or an audit.
- Re-send ("relance") of the Information on its relay channels (Telegram, e-mail)
  to ONLY the recipients who have not read it yet. It never re-notifies people who
  already read, and it is a manual, deliberate action (no automatic loop), so it
  cannot spam the audience.
"""
from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from . import db
from .information import _info_ou_404
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["informations"])


@router.get("/informations/{info_id}/export")
def export_csv(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.consulter"))]) -> Response:
    """Per-recipient delivery status of an Information, as a CSV file."""
    info = _info_ou_404(info_id, user.role)
    rows = db.fetch_all(
        """
        SELECT COALESCE(m.nom_affiche, TRIM(CONCAT(m.prenoms, ' ', m.nom))) AS nom,
               m.matricule, m.email, d.statut, d.lu_le, d.confirme_le
        FROM information_destinataire d JOIN membre m ON m.id = d.membre_id
        WHERE d.information_id = %s
        ORDER BY d.statut, nom
        """,
        (info_id,), role=user.role,
    )
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nom", "Matricule", "E-mail", "Statut", "Lu le", "Confirme le"])
    for r in rows or []:
        w.writerow([
            r.get("nom") or "", r.get("matricule") or "", r.get("email") or "",
            {"envoye": "Envoye", "lu": "Lu", "confirme": "Confirme"}.get(str(r.get("statut")), str(r.get("statut") or "")),
            r["lu_le"].strftime("%Y-%m-%d %H:%M") if r.get("lu_le") else "",
            r["confirme_le"].strftime("%Y-%m-%d %H:%M") if r.get("confirme_le") else "",
        ])
    titre = str(info.get("titre") or "information").replace('"', "").replace("\n", " ")[:40]
    # A leading BOM makes Excel open the UTF-8 file with the right accents.
    contenu = "﻿" + buf.getvalue()
    return Response(
        content=contenu.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="diffusion-{titre}.csv"'},
    )


@router.post("/informations/{info_id}/relance")
def relance(info_id: str, user: Annotated[UserMe, Depends(require_permission("informations.gerer"))]) -> dict[str, Any]:
    """Re-send the Information on its relay channels to the recipients who have NOT
    read it yet. Never touches those who already read; manual action only."""
    info = _info_ou_404(info_id, user.role)
    if info.get("statut") != "envoye":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Seule une information envoyée peut être relancée.")
    non_lus = db.fetch_all(
        "SELECT membre_id FROM information_destinataire WHERE information_id = %s AND statut NOT IN ('lu','confirme')",
        (info_id,), role=user.role,
    )
    ids = [str(r["membre_id"]) for r in (non_lus or [])]
    if not ids:
        return {"ok": True, "non_lus": 0, "telegram": {"envoyes": 0}, "email": {"envoyes": 0}}
    canaux = info.get("canaux")
    if isinstance(canaux, str):
        import json
        canaux = json.loads(canaux or "[]")
    liste = canaux or ["application", "telegram"]
    from . import information_diffusion
    telegram = information_diffusion.diffuser_telegram(info_id, info, ids, user.role) if "telegram" in liste else {"envoyes": 0, "eligibles": 0, "tronques": 0}
    email = information_diffusion.diffuser_email(info, ids, user.role) if "email" in liste else {"envoyes": 0, "eligibles": 0, "tronques": 0}
    return {"ok": True, "non_lus": len(ids), "telegram": telegram, "email": email}
