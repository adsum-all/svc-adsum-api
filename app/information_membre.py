"""What a member sees of the Informations they were sent.

Split out of ``information.py`` to keep it within the size the project allows. The
feed deliberately carries no media payload: an information stores its audio, image and
document inline as base64, so a handful of them would push the response past the
serverless body limit. The list says which media exist; opening one information
fetches them.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from . import db
from .auth import current_user
from .information import _info_dict, _info_dict_liste
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["informations"])


# --- Member: feed -----------------------------------------------------------

def _membre_ou_403(user: UserMe) -> str:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="compte non lié à un membre")
    return user.membre_id


_FEED_COLONNES = (
    "i.id, i.titre, i.sous_titre, i.contenu, i.priorite, i.auteur, i.statut, i.requiert_accuse, "
    "i.lecture_vocale_auto, i.lien_url, i.action_label, i.action_url, "
    "i.publier_le, i.expire_le, i.epingle_jusqu, i.cibles, i.cree_le, i.envoye_le, i.signature, i.canaux, "
    "i.protege, i.institutionnelle, i.affiche_entete, "
    "d.statut AS d_statut, d.lu_le, d.confirme_le"
)
_FEED_FROM = (
    " FROM information_destinataire d JOIN information i ON i.id = d.information_id "
    "WHERE d.membre_id = %s AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now())"
)
# The member's feed carries no media payload: an information can weigh megabytes of
# inline base64, and a feed of a few of them would exceed the serverless body limit.
# It advertises which media exist; opening one information fetches them.
_FEED_SELECT = (
    "SELECT " + _FEED_COLONNES + ", (i.audio_url IS NOT NULL) AS a_audio, (i.image_url IS NOT NULL) AS a_image, "
    "(i.document_url IS NOT NULL) AS a_document, (i.signature_url IS NOT NULL) AS a_signature" + _FEED_FROM
)
# One information, media included.
_FEED_SELECT_DETAIL = (
    "SELECT " + _FEED_COLONNES + ", i.audio_url, i.image_url, i.document_url, i.signature_url" + _FEED_FROM
)


@router.get("/membres/me/informations")
def feed_membre(user: Annotated[UserMe, Depends(current_user)]) -> list[dict[str, Any]]:
    mid = _membre_ou_403(user)
    rows = db.fetch_all(
        _FEED_SELECT + " ORDER BY (i.priorite = 'urgente') DESC, "
        "(i.epingle_jusqu IS NOT NULL AND i.epingle_jusqu > now()) DESC, "
        "(i.priorite = 'importante') DESC, i.envoye_le DESC",
        (mid,), role=user.role,
    )
    out = []
    for r in rows:
        d = _info_dict_liste(r)
        d.pop("cibles", None)  # the targeting is internal; a member never sees who else was targeted.
        d.update({"lu": r.get("d_statut") in ("lu", "confirme"), "confirme": r.get("d_statut") == "confirme",
                  "lu_le": r.get("lu_le"), "confirme_le": r.get("confirme_le")})
        out.append(d)
    return out


@router.get("/membres/me/informations/compteur")
def compteur_non_lus(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, int]:
    mid = _membre_ou_403(user)
    r = db.fetch_one(
        "SELECT count(*) AS n FROM information_destinataire d JOIN information i ON i.id = d.information_id "
        "WHERE d.membre_id = %s AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now()) "
        "AND d.statut NOT IN ('lu', 'confirme')",
        (mid,), role=user.role,
    ) or {}
    return {"non_lus": int(r.get("n") or 0)}


@router.get("/membres/me/informations/{info_id}")
def detail_membre(info_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    mid = _membre_ou_403(user)
    rows = db.fetch_all(_FEED_SELECT_DETAIL + " AND i.id = %s", (mid, info_id), role=user.role)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="information indisponible")
    # Opening the detail records the read (never a push or a list view). Never
    # downgrades an existing confirmation.
    db.execute(
        "UPDATE information_destinataire SET statut = 'lu', lu_le = coalesce(lu_le, now()) "
        "WHERE information_id = %s AND membre_id = %s AND statut NOT IN ('lu', 'confirme')",
        (info_id, mid), role=user.role,
    )
    r = rows[0]
    d = _info_dict(r)
    d.pop("cibles", None)
    d.update({"lu": True, "confirme": r.get("d_statut") == "confirme", "confirme_le": r.get("confirme_le")})
    return d


@router.post("/membres/me/informations/{info_id}/confirmer")
def confirmer_membre(info_id: str, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    mid = _membre_ou_403(user)
    # Confirm only an information the member actually receives, that is still sent and
    # not expired (same scope as the feed), so a confirmation can never be recorded on
    # a withdrawn or out-of-scope message.
    upd = db.execute(
        "UPDATE information_destinataire d SET statut = 'confirme', confirme_le = coalesce(d.confirme_le, now()), "
        "lu_le = coalesce(d.lu_le, now()) FROM information i "
        "WHERE d.information_id = i.id AND d.information_id = %s AND d.membre_id = %s "
        "AND i.statut = 'envoye' AND (i.expire_le IS NULL OR i.expire_le > now()) RETURNING d.confirme_le",
        (info_id, mid), role=user.role,
    )
    if not upd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="information indisponible")
    return {"ok": True, "confirme_le": upd.get("confirme_le")}
