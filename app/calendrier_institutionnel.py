# ruff: noqa: E501
"""Institutional calendar: the catholic feast catalogue, the reserved colour
referential and the reference-date occurrences that feed the member calendar.

Reference dates (institutional commemorations and catholic feasts) are NOT
activities: they carry no attendance, survey, QR or statistics. They recur every
year automatically. Movable catholic feasts are computed by ``liturgie`` and never
re-entered by hand. The occurrences builder merges institutional recurring dates,
fixed feasts (month/day) and movable feasts (computed) into a single, dated,
calendar-ready list for a given year.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import db, liturgie, sanitize
from .auth import current_user
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["calendrier-institutionnel"])

BADGE = "DATE DE RÉFÉRENCE"
_LIT_COLS = "id, cle, nom, type, mois, jour, cle_mobile, rang, categorie, couleur, description, source, visibilite, actif, ordre"


# --- Colour referential -----------------------------------------------------

@router.get("/admin/reference-couleurs")
def lister_couleurs(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT cle, libelle, hex, categorie, icone, actif, ordre FROM reference_couleur ORDER BY ordre, libelle", (), role=user.role)
    return [{"cle": r["cle"], "libelle": r["libelle"], "hex": r["hex"], "categorie": r["categorie"], "icone": r.get("icone"), "actif": bool(r["actif"])} for r in rows]


# --- Liturgical catalogue ---------------------------------------------------

class LiturgiqueIn(BaseModel):
    nom: str = Field(min_length=1, max_length=200)
    type: str = Field(default="fixe", pattern="^(fixe|mobile)$")
    mois: int | None = Field(default=None, ge=1, le=12)
    jour: int | None = Field(default=None, ge=1, le=31)
    cle_mobile: str | None = Field(default=None, max_length=40)
    rang: str = Field(default="memoire", max_length=40)
    couleur: str = Field(default="lit_blanc", max_length=40)
    description: str | None = Field(default=None, max_length=40000)
    source: str | None = Field(default=None, max_length=500)
    visibilite: str = Field(default="membre", pattern="^(interne|membre|administrateur)$")
    actif: bool = True


class LiturgiquePatch(BaseModel):
    nom: str | None = Field(default=None, max_length=200)
    couleur: str | None = Field(default=None, max_length=40)
    rang: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=40000)
    source: str | None = Field(default=None, max_length=500)
    visibilite: str | None = Field(default=None, pattern="^(interne|membre|administrateur)$")
    actif: bool | None = None


def _lit_dict(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(r["id"]), "cle": r.get("cle"), "nom": r.get("nom"), "type": r.get("type"),
        "mois": r.get("mois"), "jour": r.get("jour"), "cle_mobile": r.get("cle_mobile"),
        "rang": r.get("rang"), "categorie": r.get("categorie"), "couleur": r.get("couleur"),
        "description": r.get("description"), "source": r.get("source"),
        "visibilite": r.get("visibilite"), "actif": bool(r.get("actif")), "ordre": r.get("ordre"),
    }


@router.get("/admin/calendrier-liturgique")
def lister_liturgique(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> list[dict[str, Any]]:
    rows = db.fetch_all(f"SELECT {_LIT_COLS} FROM date_liturgique ORDER BY type, ordre, COALESCE(mois, 0), COALESCE(jour, 0), nom", (), role=user.role)
    return [_lit_dict(r) for r in rows]


@router.post("/admin/calendrier-liturgique", status_code=status.HTTP_201_CREATED)
def creer_liturgique(payload: LiturgiqueIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    if payload.type == "fixe" and (payload.mois is None or payload.jour is None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Une fête fixe requiert un mois et un jour.")
    if payload.type == "mobile":
        if not payload.cle_mobile or payload.cle_mobile not in liturgie.CLES_MOBILES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fête mobile inconnue du moteur liturgique.")
    import uuid as _uuid
    cle = f"custom_{_uuid.uuid4().hex[:10]}"
    row = db.execute(
        f"INSERT INTO date_liturgique (cle, nom, type, mois, jour, cle_mobile, rang, couleur, description, source, visibilite, actif, maj_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        f"RETURNING {_LIT_COLS}",
        (cle, payload.nom, payload.type, payload.mois, payload.jour, payload.cle_mobile, payload.rang, payload.couleur, sanitize.sanitize_html(payload.description), payload.source, payload.visibilite, payload.actif, user.id),
        role=user.role,
    )
    return _lit_dict(row or {})


@router.patch("/admin/calendrier-liturgique/{lit_id}")
def maj_liturgique(lit_id: str, payload: LiturgiquePatch, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, Any]:
    champs = payload.model_dump(exclude_unset=True)
    if not champs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune modification.")
    if "description" in champs:
        champs["description"] = sanitize.sanitize_html(champs["description"])
    sets = ", ".join(f"{k} = %s" for k in champs)
    row = db.execute(
        f"UPDATE date_liturgique SET {sets}, maj_par = %s, maj_le = now() WHERE id = %s RETURNING {_LIT_COLS}",
        (*champs.values(), user.id, lit_id), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fête introuvable.")
    return _lit_dict(row)


@router.delete("/admin/calendrier-liturgique/{lit_id}", status_code=204)
def supprimer_liturgique(lit_id: str, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> None:
    # Only custom entries can be deleted; seeded feasts are disabled via actif=false.
    row = db.fetch_one("SELECT cle FROM date_liturgique WHERE id = %s", (lit_id,), role=user.role)
    if row and not str(row.get("cle") or "").startswith("custom_"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Une fête du calendrier romain ne se supprime pas ; désactivez-la.")
    db.execute("DELETE FROM date_liturgique WHERE id = %s", (lit_id,), role=user.role)


# --- Occurrences (the calendar feed) ----------------------------------------

def _date_iso(annee: int, mois: int | None, jour: int | None) -> str | None:
    if not mois or not jour:
        return None
    try:
        return date(annee, mois, jour).isoformat()
    except ValueError:
        return None  # e.g. 29 February on a non-leap year


def _occurrence_institution(r: dict[str, Any], annee: int) -> dict[str, Any] | None:
    df = r.get("date_fixe")
    if df is not None and not r.get("repetition_annuelle"):
        if df.year != annee:
            return None
        date_iso: str | None = df.isoformat()
    elif r.get("mois") and r.get("jour"):
        date_iso = _date_iso(annee, r.get("mois"), r.get("jour"))
    elif df is not None:
        date_iso = _date_iso(annee, df.month, df.day)
    else:
        date_iso = None
    if not date_iso:
        return None
    anciennete = (annee - int(r["annee_origine"])) if r.get("annee_origine") else None
    return {
        "source_id": str(r["id"]), "origine": "institution", "categorie": r.get("categorie") or "institution",
        "type": r.get("type"), "titre": r.get("nom"), "date": date_iso, "couleur": r.get("couleur"),
        "priorite": r.get("priorite"), "rang": None, "toute_journee": bool(r.get("toute_journee")),
        "heure_debut": r["heure_debut"].isoformat() if r.get("heure_debut") else None,
        "heure_fin": r["heure_fin"].isoformat() if r.get("heure_fin") else None,
        "anciennete": anciennete, "description": r.get("description"), "image_url": r.get("image_url"),
        "lieu": r.get("lieu"), "lien": r.get("lien"), "message_membre": r.get("message_membre"),
        "source": r.get("source"), "badge": BADGE, "est_activite": False,
    }


def _occurrences_institution(annee: int, role: str | None, membre: bool) -> list[dict[str, Any]]:
    where = "WHERE actif AND afficher_calendrier"
    if membre:
        where += " AND statut = 'publie' AND visibilite IN ('membre','interne')"
    rows = db.fetch_all(
        "SELECT id, nom, description, type, date_fixe, mois, jour, annee_origine, repetition_annuelle, couleur, priorite, "
        "statut, categorie, toute_journee, heure_debut, heure_fin, lieu, lien, image_url, message_membre, source, visibilite "
        f"FROM date_institutionnelle {where}",
        (), role=role,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        occ = _occurrence_institution(r, annee)
        if occ:
            out.append(occ)
    return out


def _occurrences_liturgie(annee: int, role: str | None, membre: bool) -> list[dict[str, Any]]:
    where = "WHERE actif"
    if membre:
        where += " AND visibilite IN ('membre','interne')"
    rows = db.fetch_all(f"SELECT {_LIT_COLS} FROM date_liturgique {where}", (), role=role)
    mobiles = liturgie.fetes_mobiles(annee)
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("type") == "fixe":
            date_iso = _date_iso(annee, r.get("mois"), r.get("jour"))
        else:
            d = mobiles.get(str(r.get("cle_mobile")))
            date_iso = d.isoformat() if d else None
        if not date_iso:
            continue
        out.append({
            "source_id": str(r["id"]), "origine": "liturgie", "categorie": "liturgie", "type": r.get("type"),
            "titre": r.get("nom"), "date": date_iso, "couleur": r.get("couleur"), "priorite": None,
            "rang": r.get("rang"), "toute_journee": True, "heure_debut": None, "heure_fin": None,
            "anciennete": None, "description": r.get("description"), "image_url": None, "lieu": None,
            "lien": None, "message_membre": None, "source": r.get("source"), "badge": BADGE, "est_activite": False,
        })
    return out


def _couleurs_hex(role: str | None) -> dict[str, str]:
    rows = db.fetch_all("SELECT cle, hex FROM reference_couleur", (), role=role)
    return {str(r["cle"]): str(r["hex"]) for r in rows}


def occurrences(annee: int, role: str | None, membre: bool) -> list[dict[str, Any]]:
    """All reference-date occurrences (institutional + liturgical) for a year, sorted,
    each carrying its resolved colour hex so any client renders it without a lookup."""
    out = _occurrences_institution(annee, role, membre) + _occurrences_liturgie(annee, role, membre)
    hexs = _couleurs_hex(role)
    for o in out:
        o["couleur_hex"] = hexs.get(str(o.get("couleur")), "#8a5a12")
    out.sort(key=lambda o: (str(o["date"]), str(o.get("titre") or "")))
    return out


def _alertes(occ: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Preview coherence checks: collisions, missing description/colour."""
    alertes: list[dict[str, str]] = []
    par_date: dict[str, list[dict[str, Any]]] = {}
    for o in occ:
        par_date.setdefault(str(o["date"]), []).append(o)
    for jour, items in sorted(par_date.items()):
        if len(items) > 1:
            titres = ", ".join(str(i.get("titre")) for i in items[:4])
            alertes.append({"niveau": "info", "message": f"{len(items)} dates de référence le {jour} : {titres}."})
    sans_desc = [o for o in occ if o["origine"] == "institution" and not (o.get("description") or "").strip()]
    if sans_desc:
        alertes.append({"niveau": "avertissement", "message": f"{len(sans_desc)} date(s) institutionnelle(s) sans description."})
    sans_couleur = [o for o in occ if not o.get("couleur")]
    if sans_couleur:
        alertes.append({"niveau": "avertissement", "message": f"{len(sans_couleur)} date(s) sans couleur de catégorie."})
    return alertes


@router.get("/admin/calendrier-institutionnel/apercu")
def apercu(annee: int, user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, Any]:
    """Full preview for a given year: computed occurrences plus coherence alerts."""
    if annee < 1900 or annee > 2200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Année hors limites.")
    occ = occurrences(annee, user.role, membre=False)
    return {"annee": annee, "occurrences": occ, "alertes": _alertes(occ),
            "resume": {"total": len(occ), "institution": sum(1 for o in occ if o["origine"] == "institution"), "liturgie": sum(1 for o in occ if o["origine"] == "liturgie")}}


@router.get("/membres/me/calendrier/dates-reference")
def dates_reference_membre(annee: int, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """Member-visible reference-date occurrences for a year (institutional published +
    active liturgical). These never carry attendance, survey or QR."""
    if annee < 1900 or annee > 2200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Année hors limites.")
    return {"annee": annee, "occurrences": occurrences(annee, user.role, membre=True)}
