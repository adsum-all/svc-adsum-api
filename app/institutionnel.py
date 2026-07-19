# ruff: noqa: E501
"""Institutional identity and reference dates, configurable per organisation.

Identity is a whitelisted key/value set stored in ``integration_config`` (nothing
organisation-specific is hardcoded, so ADSUM can be redeployed for any association).
Reference dates (anniversaries, commemorations, patron feast) live in
``date_institutionnelle``: they are NOT activities (no attendance, no survey, no QR),
they recur yearly and surface in the member calendar as a distinct category. The
liturgical calendar, colour referential and the calendar occurrences feed live in
``calendrier_institutionnel``.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from . import channels, db, sanitize
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["institutionnel"])

# Full institutional identity, whitelisted. Extendable without a migration because
# integration_config is key/value. Members only ever see the PUBLIC subset below.
CLES_IDENTITE: tuple[str, ...] = (
    "org_nom", "org_nom_court", "org_acronyme", "org_site", "org_email", "org_telephone",
    "org_whatsapp", "org_adresse", "org_pays", "org_ville", "org_langue",
    "org_slogan", "org_signature", "org_description_courte", "org_description_longue",
    "org_mission", "org_vision", "org_valeurs", "org_texte_historique", "org_source_validee",
    "org_logo_url", "org_banniere_url", "org_reseaux",
    "org_fondation_date", "org_fondation_lieu", "org_fondateur_membre_id", "org_fondateur_nom",
    "org_fondateur_type", "org_saint_patron", "org_saint_patron_date", "org_fuseau", "org_contact",
)
CLES_IDENTITE_PUBLIQUES: tuple[str, ...] = (
    "org_nom", "org_nom_court", "org_acronyme", "org_site", "org_slogan", "org_signature",
    "org_description_courte", "org_mission", "org_vision", "org_saint_patron",
    "org_logo_url", "org_banniere_url", "org_fondation_date", "org_fuseau",
)
_MAX_VALEUR = 40000  # long rich descriptions are allowed


class IdentitePayload(BaseModel):
    valeurs: dict[str, str] = Field(default_factory=dict)


@router.get("/admin/parametres/identite-institutionnelle")
def lire_identite(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, str]:
    """Full organisation identity (editable, never hardcoded)."""
    return {cle: (channels.integration_value(cle) or "") for cle in CLES_IDENTITE}


@router.put("/admin/parametres/identite-institutionnelle", status_code=204)
def maj_identite(payload: IdentitePayload, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    for cle, valeur in payload.valeurs.items():
        if cle not in CLES_IDENTITE:
            continue
        val = str(valeur or "")[:_MAX_VALEUR]
        db.execute(
            "INSERT INTO integration_config (cle, valeur, maj_par, maj_le) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
            (cle, val, user.id), role=user.role,
        )


# --- Reference dates (institutional, not activities) ------------------------

class DateRefIn(BaseModel):
    nom: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=_MAX_VALEUR)
    type: str = Field(default="commemoration", max_length=40)
    date_fixe: str | None = None
    mois: int | None = Field(default=None, ge=1, le=12)
    jour: int | None = Field(default=None, ge=1, le=31)
    annee_origine: int | None = Field(default=None, ge=1, le=3000)
    repetition_annuelle: bool = True
    afficher_calendrier: bool = True
    couleur: str = Field(default="inst_commemoration", max_length=40)
    priorite: str = Field(default="normale", pattern="^(normale|importante|solennelle)$")
    statut: str = Field(default="publie", pattern="^(brouillon|publie|archive)$")
    categorie: str = Field(default="institution", max_length=40)
    toute_journee: bool = True
    heure_debut: str | None = None
    heure_fin: str | None = None
    lieu: str | None = Field(default=None, max_length=300)
    lien: str | None = Field(default=None, max_length=500)
    image_url: str | None = Field(default=None, max_length=1000)
    message_membre: str | None = Field(default=None, max_length=_MAX_VALEUR)
    source: str | None = Field(default=None, max_length=500)
    note_admin: str | None = Field(default=None, max_length=2000)
    rappel_jours: int = Field(default=0, ge=0, le=365)
    visibilite: str = Field(default="membre", pattern="^(interne|membre|administrateur)$")
    actif: bool = True

    @field_validator("lien")
    @classmethod
    def _lien_schema(cls, v: str | None) -> str | None:
        if v and not v.lower().startswith(("http://", "https://")):
            raise ValueError("Le lien doit commencer par http:// ou https://")
        return v

    @field_validator("image_url")
    @classmethod
    def _image_schema(cls, v: str | None) -> str | None:
        if v and not v.lower().startswith(("https://", "data:image/")):
            raise ValueError("L'image doit être une URL https ou une donnée data:image/")
        return v


_DATE_COLS = "id, nom, description, type, date_fixe, mois, jour, annee_origine, repetition_annuelle, afficher_calendrier, couleur, priorite, statut, categorie, toute_journee, heure_debut, heure_fin, lieu, lien, image_url, message_membre, source, note_admin, rappel_jours, visibilite, actif"


def date_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "nom": r.get("nom"), "description": r.get("description"), "type": r.get("type"),
        "date_fixe": r["date_fixe"].isoformat() if r.get("date_fixe") else None,
        "mois": r.get("mois"), "jour": r.get("jour"), "annee_origine": r.get("annee_origine"),
        "repetition_annuelle": bool(r.get("repetition_annuelle")), "afficher_calendrier": bool(r.get("afficher_calendrier")),
        "couleur": r.get("couleur"), "priorite": r.get("priorite"), "statut": r.get("statut"), "categorie": r.get("categorie"),
        "toute_journee": bool(r.get("toute_journee")),
        "heure_debut": r["heure_debut"].isoformat() if r.get("heure_debut") else None,
        "heure_fin": r["heure_fin"].isoformat() if r.get("heure_fin") else None,
        "lieu": r.get("lieu"), "lien": r.get("lien"), "image_url": r.get("image_url"),
        "message_membre": r.get("message_membre"), "source": r.get("source"), "note_admin": r.get("note_admin"),
        "rappel_jours": r.get("rappel_jours"), "visibilite": r.get("visibilite"), "actif": bool(r.get("actif")),
    }


def _params(p: DateRefIn) -> tuple[Any, ...]:
    # description and message_membre are authored in a rich editor and rendered as
    # HTML in the member app, so they are sanitised (allowlist) against stored XSS.
    return (
        p.nom, sanitize.sanitize_html(p.description), p.type, p.date_fixe or None, p.mois, p.jour, p.annee_origine,
        p.repetition_annuelle, p.afficher_calendrier, p.couleur, p.priorite, p.statut, p.categorie,
        p.toute_journee, p.heure_debut or None, p.heure_fin or None, p.lieu, p.lien, p.image_url,
        sanitize.sanitize_html(p.message_membre), p.source, p.note_admin, p.rappel_jours, p.visibilite, p.actif,
    )


@router.get("/admin/dates-institutionnelles")
def lister_dates(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all(f"SELECT {_DATE_COLS} FROM date_institutionnelle ORDER BY COALESCE(mois, 99), COALESCE(jour, 99), nom", (), role=user.role)
    return [date_dict(r) for r in rows]


@router.post("/admin/dates-institutionnelles")
def creer_date(payload: DateRefIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    row = db.execute(
        "INSERT INTO date_institutionnelle "
        "(nom, description, type, date_fixe, mois, jour, annee_origine, repetition_annuelle, afficher_calendrier, couleur, priorite, statut, categorie, toute_journee, heure_debut, heure_fin, lieu, lien, image_url, message_membre, source, note_admin, rappel_jours, visibilite, actif, maj_par, maj_le) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        f"RETURNING {_DATE_COLS}",
        (*_params(payload), user.id), role=user.role,
    )
    return date_dict(row or {})


@router.patch("/admin/dates-institutionnelles/{date_id}")
def maj_date(date_id: str, payload: DateRefIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    row = db.execute(
        "UPDATE date_institutionnelle SET "
        "nom=%s, description=%s, type=%s, date_fixe=%s, mois=%s, jour=%s, annee_origine=%s, repetition_annuelle=%s, afficher_calendrier=%s, couleur=%s, priorite=%s, statut=%s, categorie=%s, toute_journee=%s, heure_debut=%s, heure_fin=%s, lieu=%s, lien=%s, image_url=%s, message_membre=%s, source=%s, note_admin=%s, rappel_jours=%s, visibilite=%s, actif=%s, maj_par=%s, maj_le=now() "
        f"WHERE id=%s RETURNING {_DATE_COLS}",
        (*_params(payload), user.id, date_id), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Date introuvable.")
    return date_dict(row)


@router.delete("/admin/dates-institutionnelles/{date_id}", status_code=204)
def supprimer_date(date_id: str, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    db.execute("DELETE FROM date_institutionnelle WHERE id = %s", (date_id,), role=user.role)


@router.get("/membres/me/identite-institutionnelle")
def identite_membre(user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """Public-facing institutional identity plus the member-visible reference dates."""
    identite = {cle: (channels.integration_value(cle) or "") for cle in CLES_IDENTITE_PUBLIQUES}
    dates = db.fetch_all(
        f"SELECT {_DATE_COLS} FROM date_institutionnelle WHERE actif AND statut = 'publie' AND visibilite IN ('membre','interne') "
        "ORDER BY COALESCE(mois, 99), COALESCE(jour, 99)",
        (), role=user.role,
    )
    return {"identite": identite, "dates": [date_dict(r) for r in dates]}
