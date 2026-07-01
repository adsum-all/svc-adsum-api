"""Notification engine: one entry point that respects the admin catalogue, the
member's channel and category preferences, and the member's language.

`notifier(...)` renders the right bilingual template, delivers it over every
enabled channel (in-app always, plus e-mail, Telegram, WhatsApp when configured
and opted-in) and, for scheduled sends, logs it so a member is never notified
twice for the same thing. Individual senders never raise into the caller.

Also holds the daily scheduled job (`/cron/quotidien`) that fans out birthday
wishes, day-before activity reminders, the Monday agenda and the Sunday recap,
all deduplicated. A single daily cron keeps it within the free hosting tier.
"""
# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from . import audit, channels, db
from .cron_auth import require_cron_auth
from .deps import require_roles
from .email_templates import render_anniversaire_email, render_notification_email
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["notifications"])

require_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")

# Type -> member preference category that gates it (None = always sent, critical).
_CATEGORY_PREF = {
    "compte_cree": None,
    "otp": None,
    "inscription_decision": None,
    "annonce": None,
    "demande_reponse": "demandes",
    "modification_decision": "demandes",
    "document_demande": "demandes",
    "activite_rappel_j1": "rappels",
    "activite_rappel_start": "rappels",
    "agenda_hebdo": "rappels",
    "recap_hebdo": "rappels",
    "activite_demarree": "evenements",
    "activite_modifiee": "evenements",
    "questionnaire_disponible": "evenements",
    "anniversaire": "anniversaire",
    "anniversaire_pairs": "anniv_pairs",
    "activite_test_diffusion": "evenements",
    "inscription_soumise": None,
    "engagement_code": None,
    "attestation_requise": None,
    "attestation_rappel": "rappels",
    "attestation_expiree": None,
    "correction_demandee": None,
}

# Minimal built-in fallbacks (FR/EN) used only if no template row exists yet.
_FALLBACK = {
    "fr": ("Notification ADSUM", "Vous avez une nouvelle notification."),
    "en": ("ADSUM notification", "You have a new notification."),
}


def _subst(text: str, ctx: dict[str, object]) -> str:
    for key, value in ctx.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _render(type_cle: str, lang: str, ctx: dict[str, object], role: str | None) -> tuple[str, str]:
    row = db.fetch_one(
        "SELECT titre, corps, titre_en, corps_en FROM modele_message WHERE cle = %s",
        (type_cle,),
        role=role,
    )
    if not row:
        titre, corps = _FALLBACK["en" if lang == "en" else "fr"]
        return _subst(titre, ctx), _subst(corps, ctx)
    if lang == "en" and row.get("titre_en"):
        titre, corps = row["titre_en"], row.get("corps_en") or row["corps"]
    else:
        titre, corps = row["titre"], row["corps"]
    return _subst(str(titre), ctx), _subst(str(corps or ""), ctx)


def notifier(
    membre_id: str,
    role: str | None,
    type_cle: str,
    ctx: dict[str, object] | None = None,
    ref_id: str = "",
    dedup: bool = False,
    whatsapp_params: list[str] | None = None,
) -> list[str]:
    """Send one notification honouring admin toggle, member prefs and language."""
    ctx = ctx or {}
    try:
        active = db.fetch_one("SELECT actif FROM type_notification WHERE cle = %s", (type_cle,), role=role)
        if active and not active["actif"]:
            return []
        member = db.fetch_one("SELECT langue FROM membre WHERE id = %s", (membre_id,), role=role)
        lang = (member["langue"] if member and member.get("langue") else "fr") or "fr"
        pref_col = _CATEGORY_PREF.get(type_cle)
        if pref_col:
            pref = db.fetch_one(f"SELECT {pref_col} FROM preference_notification WHERE membre_id = %s", (membre_id,), role=role)
            if pref and not pref[pref_col]:
                return []
        if dedup:
            # Reserve the log row BEFORE sending. If a concurrent run already
            # claimed it, RETURNING yields nothing and we skip: the unique
            # constraint makes the send exactly-once even under overlapping crons.
            reserved = db.fetch_one(
                "INSERT INTO notification_log (membre_id, type_cle, ref_id, canaux) VALUES (%s, %s, %s, '') "
                "ON CONFLICT (membre_id, type_cle, ref_id) DO NOTHING RETURNING id",
                (membre_id, type_cle, ref_id),
                role=role,
            )
            if not reserved:
                return []
        titre, corps = _render(type_cle, lang, ctx, role)
        signature = channels.integration_value("signature") or "Sacerdoce Royal"
        site = channels.integration_value("site_officiel")
        # Plain-text footer for Telegram / in-app (the birthday template already signs off).
        footer = ("\n\n" + (site if site else "")) if type_cle == "anniversaire" else ("\n\n" + signature + (f"\n{site}" if site else ""))
        corps_text = corps + (footer if footer.strip() else "")
        if type_cle == "anniversaire":
            html = render_anniversaire_email(titre, corps, site=site or None)
        else:
            html = render_notification_email(titre, corps, signature=signature, site=site or None)
        msg = channels.Message(titre=titre, corps_text=corps_text, corps_html=html, type_notif=type_cle)
        used = channels.dispatch(membre_id, role, msg, whatsapp_params=whatsapp_params)
        if dedup:
            db.execute(
                "UPDATE notification_log SET canaux = %s WHERE membre_id = %s AND type_cle = %s AND ref_id = %s",
                (",".join(used), membre_id, type_cle, ref_id),
                role=role,
            )
        return used
    except Exception:  # noqa: BLE001 - a notification must never break the caller
        return []


def _prenom(row: dict[str, object]) -> str:
    return (str(row.get("prenoms") or "").split(" ")[0]) or "cher membre"


# --- Daily scheduled job ----------------------------------------------------

def _run_quotidien(role: str | None) -> dict[str, object]:
    now = datetime.now(tz=UTC)
    weekday = now.weekday()  # Monday = 0, Sunday = 6
    annee = now.year
    result = {"anniversaires": 0, "rappels_j1": 0, "agenda": 0, "recap": 0, "digest_pairs": 0}

    # 1) Birthdays today.
    for r in db.fetch_all(
        "SELECT id, prenoms FROM membre WHERE date_naissance IS NOT NULL AND statut = 'actif' "
        "AND extract(month from date_naissance) = extract(month from now()) "
        "AND extract(day from date_naissance) = extract(day from now())",
        (),
        role=role,
    ):
        prenom = _prenom(r)
        if notifier(str(r["id"]), role, "anniversaire", {"prenom": prenom}, ref_id=str(annee), dedup=True, whatsapp_params=[prenom]):
            result["anniversaires"] += 1

    # 1b) Peer digest: "today's birthdays" to members who opted into it. Only
    # celebrants who kept their birthday visible in the directory are listed.
    celebrants = db.fetch_all(
        "SELECT prenoms, nom FROM membre WHERE date_naissance IS NOT NULL AND statut = 'actif' "
        "AND anniversaire_visible_annuaire = true "
        "AND extract(month from date_naissance) = extract(month from now()) "
        "AND extract(day from date_naissance) = extract(day from now()) ORDER BY prenoms",
        (),
        role=role,
    )
    if celebrants:
        liste = ", ".join(f"{c['prenoms'] or ''} {c['nom'] or ''}".strip() for c in celebrants)
        ref = now.strftime("%Y-%m-%d") + "-pairs"
        for m in db.fetch_all("SELECT id FROM membre WHERE statut = 'actif'", (), role=role):
            if notifier(str(m["id"]), role, "anniversaire_pairs", {"liste": liste}, ref_id=ref, dedup=True):
                result["digest_pairs"] += 1

    # 2) Day-before reminders: events starting in the next 24-36h.
    evs = db.fetch_all(
        "SELECT id, titre, debut FROM evenement WHERE debut IS NOT NULL "
        "AND debut BETWEEN now() + interval '12 hours' AND now() + interval '36 hours'",
        (),
        role=role,
    )
    actifs = db.fetch_all("SELECT id, prenoms, langue FROM membre WHERE statut = 'actif'", (), role=role)
    for ev in evs:
        date_str = ev["debut"].strftime("%d/%m/%Y a %Hh%M") if ev["debut"] else ""
        for m in actifs:
            ctx = {"prenom": _prenom(m), "titre": ev["titre"], "date": date_str}
            if notifier(str(m["id"]), role, "activite_rappel_j1", ctx, ref_id=str(ev["id"]), dedup=True):
                result["rappels_j1"] += 1

    # 3) Monday: weekly agenda of the coming week.
    if weekday == 0:
        semaine = db.fetch_all(
            "SELECT titre, debut FROM evenement WHERE debut BETWEEN now() AND now() + interval '7 days' ORDER BY debut ASC",
            (),
            role=role,
        )
        if semaine:
            liste = "; ".join(f"{e['titre']} ({e['debut'].strftime('%a %d/%m %Hh%M')})" for e in semaine if e["debut"])
            ref = now.strftime("%G-W%V")
            for m in actifs:
                if notifier(str(m["id"]), role, "agenda_hebdo", {"prenom": _prenom(m), "liste": liste}, ref_id=ref, dedup=True):
                    result["agenda"] += 1

    # 4) Sunday: recap of the past week.
    if weekday == 6:
        passe = db.fetch_all(
            "SELECT titre FROM evenement WHERE debut BETWEEN now() - interval '7 days' AND now() ORDER BY debut ASC",
            (),
            role=role,
        )
        if passe:
            liste = "; ".join(str(e["titre"]) for e in passe)
            ref = now.strftime("%G-W%V") + "-recap"
            for m in actifs:
                if notifier(str(m["id"]), role, "recap_hebdo", {"prenom": _prenom(m), "liste": liste}, ref_id=ref, dedup=True):
                    result["recap"] += 1

    # 5) Manual-attestation follow-up: strategic reminders and expiry.
    result["attest_rappels"], result["attest_expirees"] = _scan_attestations(role)

    return {"ok": True, **result}


def _scan_attestations(role: str | None) -> tuple[int, int]:
    """Strategic reminders (14/7/2/0 days before) and automatic invalidation of
    hand-signed attestations that were never returned by the deadline."""
    rappels = expirees = 0
    # Reminders at discrete milestones (not daily), deduplicated per milestone.
    pending = db.fetch_all(
        "SELECT a.id, a.membre_id, m.prenoms, to_char(a.echeance, 'DD/MM/YYYY') AS ech, "
        "floor(extract(epoch FROM (a.echeance - now())) / 86400)::int AS jours "
        "FROM attestation_manuelle a JOIN membre m ON m.id = a.membre_id "
        "WHERE a.statut IN ('awaiting', 'reminded') AND a.echeance IS NOT NULL AND a.echeance >= now()",
        (),
        role=role,
    )
    for a in pending:
        jours = int(a["jours"])
        jalon = next((j for j in (14, 7, 2, 0) if jours == j), None)
        if jalon is None:
            continue
        prenom = (str(a.get("prenoms") or "").split(" ")[0]) or "cher membre"
        if notifier(str(a["membre_id"]), role, "attestation_rappel", {"prenom": prenom, "echeance": a["ech"]}, ref_id=f"{a['id']}:{jalon}", dedup=True):
            rappels += 1
            db.execute("UPDATE attestation_manuelle SET statut = 'reminded' WHERE id = %s", (a["id"],), role=role)
    # Expiry: past deadline and not returned -> invalidate the registration.
    for a in db.fetch_all(
        "SELECT a.id, a.membre_id, m.prenoms FROM attestation_manuelle a JOIN membre m ON m.id = a.membre_id "
        "WHERE a.statut IN ('awaiting', 'reminded', 'overdue') AND a.echeance IS NOT NULL AND a.echeance < now()",
        (),
        role=role,
    ):
        db.execute("UPDATE attestation_manuelle SET statut = 'invalidated' WHERE id = %s", (a["id"],), role=role)
        db.execute("UPDATE membre SET attestation_statut = 'invalidated' WHERE id = %s", (a["membre_id"],), role=role)
        prenom = (str(a.get("prenoms") or "").split(" ")[0]) or "cher membre"
        if notifier(str(a["membre_id"]), role, "attestation_expiree", {"prenom": prenom}, ref_id=f"{a['id']}:exp", dedup=True):
            expirees += 1
    return rappels, expirees


@router.get("/cron/quotidien")
def cron_quotidien(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    """Daily scheduled notifications, secured by the CRON_SECRET bearer."""
    require_cron_auth(authorization)
    return _run_quotidien(role=None)


@router.post("/admin/notifications/declencher-quotidien")
def declencher_quotidien(user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    """Run the daily job on demand (admin), for testing."""
    result = _run_quotidien(role=user.role)
    audit.log(user.id, user.role, "declenchement_notifications", "membre", None, result)
    return result


# --- Admin: notification catalogue ------------------------------------------

class TypeToggle(BaseModel):
    actif: bool


@router.get("/admin/notifications/types")
def list_types(user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    rows = db.fetch_all("SELECT cle, libelle, categorie, actif, scheduled FROM type_notification ORDER BY categorie, libelle", (), role=user.role)
    return [{"cle": r["cle"], "libelle": r["libelle"], "categorie": r["categorie"], "actif": bool(r["actif"]), "scheduled": bool(r["scheduled"])} for r in rows]


@router.put("/admin/notifications/types/{cle}")
def toggle_type(cle: str, payload: TypeToggle, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    row = db.execute("UPDATE type_notification SET actif = %s, maj_le = now() WHERE cle = %s RETURNING cle", (payload.actif, cle), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown type")
    audit.log(user.id, user.role, "toggle_type_notification", "type_notification", cle, {"actif": payload.actif})
    return {"ok": True, "actif": payload.actif}


# --- Member: language -------------------------------------------------------

class LangueIn(BaseModel):
    langue: str


@router.put("/membres/me/langue")
def set_langue(payload: LangueIn, user: Annotated[UserMe, Depends(require_roles("membre", "super_admin", "admin", "gestionnaire", "controleur", "direction"))]) -> dict[str, object]:
    if payload.langue not in ("fr", "en"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="langue must be fr or en")
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    db.execute("UPDATE membre SET langue = %s WHERE id = %s", (payload.langue, user.membre_id), role=user.role)
    return {"ok": True, "langue": payload.langue}
