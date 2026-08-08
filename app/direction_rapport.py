"""Send a direction report through the platform's communication channel.

The direction can already download a report. Sending one is a different act: it puts
the organisation's attendance figures, and on one screen its members' names, into
somebody's mailbox. That has to leave a trace, and it has to leave through the
platform rather than through a personal client, so the dispatch is attributable and
the recipient is recorded.

Two properties this rests on.

The table is rebuilt here, never trusted. The browser sends rows; the server renders
them into cells it has escaped itself, so a value carrying markup lands as text in
the message instead of as markup in the recipient's mail client.

Every send is audited before the reader is told it worked, with the recipient, the
title and the size of the table. A report that left without a trace is one nobody can
account for afterwards.
"""
from __future__ import annotations

from html import escape
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from . import audit, email_gateway
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/direction", tags=["direction"])

#: Bounds sized to what a readable report is, not to what the mail provider accepts.
#: A thousand-row table in the body of a message is not a report anybody reads, and
#: the download exists for that case.
_MAX_LIGNES = 500
_MAX_COLONNES = 30
_MAX_CELLULE = 300


class RapportEnvoi(BaseModel):
    destinataire: EmailStr
    titre: str = Field(min_length=1, max_length=160)
    colonnes: list[str] = Field(max_length=_MAX_COLONNES)
    lignes: list[list[str]] = Field(max_length=_MAX_LIGNES)
    contexte: str = Field(default="", max_length=600)


def _tronquer(v: str) -> str:
    return v[:_MAX_CELLULE]


def _rendre(rapport: RapportEnvoi) -> tuple[str, str]:
    """Build the message body, escaping every value that came from the browser."""
    titre = escape(_tronquer(rapport.titre))
    contexte = escape(_tronquer(rapport.contexte))
    entete = "".join(f"<th>{escape(_tronquer(c))}</th>" for c in rapport.colonnes)
    corps = "".join(
        "<tr>" + "".join(f"<td>{escape(_tronquer(v))}</td>" for v in ligne[:_MAX_COLONNES]) + "</tr>"
        for ligne in rapport.lignes
    )
    html = (
        '<div style="font:14px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#101427">'
        f"<h2 style=\"margin:0 0 6px\">{titre}</h2>"
        f'<p style="margin:0 0 14px;color:#5b6480;font-size:12px">{contexte}</p>'
        '<table style="border-collapse:collapse;font-size:12px">'
        f'<thead><tr style="background:#eef1f7">{entete}</tr></thead>'
        f"<tbody>{corps}</tbody></table>"
        '<p style="margin-top:18px;color:#8a92a8;font-size:11px">'
        "Rapport produit depuis l'espace Direction. Chiffres consolidés, une seule "
        "présence par membre et par activité.</p></div>"
    )

    # A plain-text part as well: a recipient reading in a client that refuses HTML
    # would otherwise receive an empty message and conclude the send failed.
    lignes_txt = [rapport.titre, rapport.contexte, "", " | ".join(rapport.colonnes)]
    lignes_txt += [" | ".join(_tronquer(v) for v in ligne) for ligne in rapport.lignes]
    return "\n".join(lignes_txt), html


@router.post("/rapport/envoyer")
def envoyer_rapport(
    rapport: RapportEnvoi,
    user: Annotated[UserMe, Depends(require_permission("statistiques.consulter"))],
) -> dict[str, Any]:
    """Send a report to one recipient, audited."""
    if not rapport.colonnes or not rapport.lignes:
        raise HTTPException(status_code=422, detail="Le rapport est vide, rien à envoyer.")

    texte, html = _rendre(rapport)
    sujet = f"[Direction] {_tronquer(rapport.titre)}"
    envoye, fournisseur = email_gateway.send_email(str(rapport.destinataire), sujet, texte, html)

    # Written whether or not the provider accepted it: a refused send is exactly the
    # event somebody needs to find later, and recording only the successes would hide
    # it.
    audit.log(
        user.id, user.role, "envoi_rapport_direction", "rapport", None,
        {
            "destinataire": str(rapport.destinataire),
            "titre": _tronquer(rapport.titre),
            "lignes": len(rapport.lignes),
            "colonnes": len(rapport.colonnes),
            "contexte": _tronquer(rapport.contexte),
            "envoye": envoye,
            "fournisseur": fournisseur,
        },
    )

    if not envoye:
        raise HTTPException(
            status_code=502,
            detail="Le fournisseur de messagerie a refusé l'envoi. La tentative est tracée.",
        )
    return {"envoye": True, "destinataire": str(rapport.destinataire), "lignes": len(rapport.lignes)}
