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


def envoyer_maintenant(rapport: RapportEnvoi, destinataire: str) -> tuple[bool, str]:
    """Render and send, without the HTTP layer.

    Shared with the scheduled channel so a report that goes out automatically is
    rendered and escaped exactly like one a person sent by hand. Two code paths
    producing the same message is how one of them ends up unescaped.
    """
    texte, html = _rendre(rapport)
    sujet = f"[Direction] {_tronquer(rapport.titre)}"
    return email_gateway.send_email(destinataire, sujet, texte, html)


@router.post("/rapport/envoyer")
def envoyer_rapport(
    rapport: RapportEnvoi,
    user: Annotated[UserMe, Depends(require_permission("statistiques.consulter"))],
) -> dict[str, Any]:
    """Send a report to one recipient, audited."""
    if not rapport.colonnes or not rapport.lignes:
        raise HTTPException(status_code=422, detail="Le rapport est vide, rien à envoyer.")

    envoye, fournisseur = envoyer_maintenant(rapport, str(rapport.destinataire))

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


# --- The automatic channel: subscriptions ----------------------------------

class AbonnementCreation(BaseModel):
    libelle: str = Field(min_length=1, max_length=160)
    destinataires: list[EmailStr] = Field(min_length=1, max_length=20)
    frequence: str = Field(description="quotidien, hebdomadaire ou mensuel")
    dimension: str = Field(default="commission")
    filtres: dict[str, Any] = Field(default_factory=dict)


_Pilote = Annotated[UserMe, Depends(require_permission("statistiques.consulter"))]


@router.get("/rapport/abonnements")
def lister_abonnements(user: _Pilote) -> list[dict[str, Any]]:
    """The reports the organisation sends on its own, and when each last went out."""
    from . import direction_planification

    return direction_planification.lister(user.role)


@router.post("/rapport/abonnements", status_code=201)
def creer_abonnement(corps: AbonnementCreation, user: _Pilote) -> dict[str, Any]:
    """Register a recurring report.

    The scope is validated now rather than at send time: a subscription accepted with
    an unknown axis would sit in the table looking healthy and fail every night with
    nobody watching.
    """
    from . import direction_analyse, direction_planification

    try:
        return direction_planification.creer(
            corps.libelle, [str(d) for d in corps.destinataires], corps.frequence,
            corps.dimension, corps.filtres, user.id, user.role,
        )
    except direction_analyse.DimensionInconnue as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@router.put("/rapport/abonnements/{abonnement_id}")
def basculer_abonnement(abonnement_id: str, actif: bool, user: _Pilote) -> dict[str, Any]:
    """Pause or resume a subscription without losing what it describes."""
    from . import direction_planification

    if not direction_planification.basculer(abonnement_id, actif, user.id, user.role):
        raise HTTPException(status_code=404, detail="Abonnement introuvable.")
    return {"id": abonnement_id, "actif": actif}


@router.delete("/rapport/abonnements/{abonnement_id}")
def supprimer_abonnement(abonnement_id: str, user: _Pilote) -> dict[str, Any]:
    from . import direction_planification

    if not direction_planification.supprimer(abonnement_id, user.id, user.role):
        raise HTTPException(status_code=404, detail="Abonnement introuvable.")
    return {"supprime": True}


@router.post("/rapport/abonnements/{abonnement_id}/tester")
def tester_abonnement(abonnement_id: str, user: _Pilote) -> dict[str, Any]:
    """Send this subscription once, now, to check it before trusting it to a schedule.

    Without this, the only way to find out whether a recurring report works is to wait
    for the night it should have gone out and notice that it did not.
    """
    from . import direction_planification

    resultat = direction_planification.executer_un(abonnement_id, force=True, role=user.role)
    if resultat is None:
        raise HTTPException(status_code=404, detail="Abonnement introuvable.")
    audit.log(
        user.id, user.role, "test_rapport_planifie", "rapport_planifie", abonnement_id, resultat,
    )
    return resultat
