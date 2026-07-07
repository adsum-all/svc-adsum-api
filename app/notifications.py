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

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from . import audit, channels, db
from .cron_auth import require_cron_auth
from .email_templates import render_anniversaire_email, render_notification_email
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["notifications"])


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
    "retention_renouvellement": None,
    "compte_bloque": None,
    "compte_debloque": None,
    "connexion_inhabituelle": None,
    "securite_alerte": None,
    "otp_expire": None,
    "modification_complement": None,
    "activite_annulee": "evenements",
    "participation_bientot_close": "rappels",
    # Attendance survey ("sondage de pointage"): MANDATORY. None means the member
    # can never turn it off, because presence tracking is an administration duty.
    "sondage_activite": None,
}

# Minimal built-in fallbacks (FR/EN) used only if no template row exists yet.
_FALLBACK = {
    "fr": ("Notification ADSUM", "Vous avez une nouvelle notification."),
    "en": ("ADSUM notification", "You have a new notification."),
}

# Maps a notification type to its signature family so an admin can sign each
# family differently (integration_config keys seeded by migration 0038).
_SIGNATURE_KEY = {
    "anniversaire": "signature_anniversaire",
    "anniversaire_pairs": "signature_anniversaire",
    "annonce": "signature_information",
    "agenda_hebdo": "signature_information",
    "recap_hebdo": "signature_information",
    "questionnaire_disponible": "signature_information",
    "inscription_soumise": "signature_accuse",
    "inscription_decision": "signature_approbation",
    "modification_decision": "signature_approbation",
    "attestation_requise": "signature_approbation",
    "correction_demandee": "signature_correction",
    "attestation_rappel": "signature_rappel",
    "attestation_expiree": "signature_rappel",
    "activite_rappel_j1": "signature_rappel",
    "activite_annulee": "signature_information",
    "participation_bientot_close": "signature_rappel",
    "modification_complement": "signature_approbation",
    # The attendance survey and the just-starting reminder are convocations, signed
    # by a dedicated authority (default "Le Moderateur"), configurable by the admin.
    "sondage_activite": "signature_convocation",
    "activite_rappel_start": "signature_convocation",
}


def _resolve_signature(type_cle: str) -> str:
    """Signature for this message family, falling back to the global one.

    Order: the family-specific signature (if the admin set one) -> the global
    ``signature`` -> the built-in default. Never returns an empty string.
    """
    key = _SIGNATURE_KEY.get(type_cle)
    specific = channels.integration_value(key) if key else ""
    return specific or channels.integration_value("signature") or "Sacerdoce Royal"


def _subst(text: str, ctx: dict[str, object]) -> str:
    for key, value in ctx.items():
        text = text.replace("{" + key + "}", str(value))
    # Defensive safety net: never leak an unsubstituted placeholder to the member.
    # Drop any remaining "{word}" and tidy the punctuation/space it leaves behind
    # (e.g. "details ici : {lien}." -> "details." ; " a {heure}." -> ".").
    if "{" in text:
        text = re.sub(r"\s*(?:ici\s*)?:?\s*\{[a-zA-Z_]+\}", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([.,;!?])", r"\1", text)
    return text.strip()


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
        active = db.fetch_one("SELECT actif, sensibilite FROM type_notification WHERE cle = %s", (type_cle,), role=role)
        sensibilite = (active or {}).get("sensibilite") or "operationnel"
        critique = sensibilite == "critique"
        # A critical security message is never silenced by an admin toggle; other
        # types can be turned off in the catalogue.
        if active and not active["actif"] and not critique:
            return []
        member = db.fetch_one("SELECT langue FROM membre WHERE id = %s", (membre_id,), role=role)
        lang = (member["langue"] if member and member.get("langue") else "fr") or "fr"
        # Sensitivity matrix: critical and private messages are always delivered
        # (they must reach the member); only operational/informational messages are
        # gated by the member's per-category preference.
        pref_col = _CATEGORY_PREF.get(type_cle)
        if pref_col and sensibilite in ("operationnel", "informationnel"):
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
        signature = _resolve_signature(type_cle)
        site = channels.integration_value("site_officiel")
        # Plain-text footer for Telegram / in-app (the birthday template already signs off).
        footer = ("\n\n" + (site if site else "")) if type_cle == "anniversaire" else ("\n\n" + signature + (f"\n{site}" if site else ""))
        corps_text = corps + (footer if footer.strip() else "")
        if type_cle == "anniversaire":
            html = render_anniversaire_email(titre, corps, site=site or None)
        else:
            html = render_notification_email(titre, corps, signature=signature, site=site or None)
        msg = channels.Message(titre=titre, corps_text=corps_text, corps_html=html, type_notif=type_cle)
        # Critical messages ignore the channel kill-switch and the member's channel
        # preferences: security must always be attempted on every reachable channel.
        used = channels.dispatch(membre_id, role, msg, whatsapp_params=whatsapp_params, critique=critique)
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
    result = {"anniversaires": 0, "rappels_j1": 0, "agenda": 0, "recap": 0, "digest_pairs": 0, "participation_close": 0}

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
    lien_app = channels.integration_value("site_officiel") or "https://adsum-web-membre.pages.dev"
    from .visibilite import CIBLE_PREDICATE

    for ev in evs:
        date_str = ev["debut"].strftime("%d/%m/%Y") if ev["debut"] else ""
        heure_str = ev["debut"].strftime("%Hh%M") if ev["debut"] else ""
        # Only the members TARGETED by this event receive the reminder: a restricted
        # event (commission/intendance/coordination/tribu) never leaks its title and
        # time to the whole base. General events still reach everyone.
        cibles = db.fetch_all(
            "SELECT m.id, m.prenoms, m.langue FROM membre m "
            "LEFT JOIN intendance mi ON mi.id = m.intendance_id "
            "JOIN evenement e ON e.id = %s "
            f"WHERE m.statut = 'actif' AND {CIBLE_PREDICATE}",
            (str(ev["id"]),),
            role=role,
        )
        for m in cibles:
            ctx = {"prenom": _prenom(m), "titre": ev["titre"], "date": date_str, "heure": heure_str, "lien": lien_app}
            if notifier(str(m["id"]), role, "activite_rappel_j1", ctx, ref_id=str(ev["id"]), dedup=True):
                result["rappels_j1"] += 1

    # 2b) Attendance form closing within the next 24h: nudge members who began a
    # declaration but never validated it (drafts), so their presence is counted.
    # A scanned member is already present, so is excluded; a validated one too.
    # Same window formula as the participation engine (admin-configured hours).
    from .participation import FENETRE_FIN_SQL

    fermetures = db.fetch_all(
        "SELECT e.id, e.titre, p.membre_id, m.prenoms "
        "FROM evenement e "
        "JOIN participation p ON p.evenement_id = e.id AND NOT p.valide AND p.source <> 'scan' "
        "JOIN membre m ON m.id = p.membre_id AND m.statut = 'actif' "
        "WHERE e.debut IS NOT NULL "
        f"AND {FENETRE_FIN_SQL} BETWEEN now() AND now() + interval '24 hours'",
        (),
        role=role,
    )
    for f in fermetures:
        ctx = {"prenom": _prenom(f), "titre": f["titre"], "lien": lien_app}
        if notifier(str(f["membre_id"]), role, "participation_bientot_close", ctx, ref_id=str(f["id"]), dedup=True):
            result["participation_close"] += 1

    # 3) Monday: weekly agenda of the coming week.
    if weekday == 0:
        # Shared digest to every active member, so it lists only community-wide
        # events; a targeted event never appears in a broadcast agenda.
        semaine = db.fetch_all(
            "SELECT titre, debut FROM evenement WHERE cible_type = 'general' "
            "AND debut BETWEEN now() AND now() + interval '7 days' ORDER BY debut ASC",
            (),
            role=role,
        )
        if semaine:
            liste = "; ".join(f"{e['titre']} ({e['debut'].strftime('%a %d/%m %Hh%M')})" for e in semaine if e["debut"])
            ref = now.strftime("%G-W%V")
            for m in actifs:
                if notifier(str(m["id"]), role, "agenda_hebdo", {"prenom": _prenom(m), "liste": liste, "lien": lien_app}, ref_id=ref, dedup=True):
                    result["agenda"] += 1

    # 4) Sunday: recap of the past week.
    if weekday == 6:
        passe = db.fetch_all(
            "SELECT titre FROM evenement WHERE cible_type = 'general' "
            "AND debut BETWEEN now() - interval '7 days' AND now() ORDER BY debut ASC",
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

    # 6) Housekeeping: delete Telegram messages past the retention window.
    purge = channels.purge_old_telegram(role)
    result["telegram_purges"] = purge.get("supprimes", 0)

    # 6b) Unlock windows: a request left in 'attente_membre' beyond its response
    # deadline is closed automatically as 'sans suite', with a visible trace and
    # the unlocked elements re-locked. The window itself is admin-configurable
    # (deblocage_delai_jours; 14-vs-30-day arbitration pending, never hardcoded).
    from .demandes import _notify_ticket, _system_message

    result["cloture_sans_suite"] = 0
    en_retard = db.fetch_all(
        "SELECT id, membre_id FROM demande WHERE statut = 'attente_membre' "
        "AND echeance_reponse IS NOT NULL AND echeance_reponse < now()",
        (),
        role=role,
    )
    for d in en_retard:
        db.execute(
            "UPDATE demande SET statut = 'refusee', "
            "motif_cloture = 'Clôturée automatiquement : aucun retour dans le délai imparti.', "
            "clos_le = now(), maj_le = now(), echeance_reponse = NULL WHERE id = %s",
            (str(d["id"]),),
            role=role,
        )
        db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (str(d["membre_id"]),), role=role)
        _system_message(str(d["id"]), role,
                        "Demande clôturée automatiquement : aucun retour du membre avant la date limite. "
                        "Les éléments débloqués ont été reverrouillés.")
        _notify_ticket(str(d["id"]), role, "Demande clôturée sans suite",
                       "Votre demande a été clôturée automatiquement car aucun retour n'a été reçu dans le délai. "
                       "Vous pouvez ouvrir une nouvelle demande si nécessaire.")
        result["cloture_sans_suite"] += 1

    # 6c) Interface notifications past the retention window are purged. This
    # NEVER touches conversation messages, requests, their history or the audit
    # journal: those live in their own tables with their own retention.
    retention = db.fetch_one(
        "SELECT coalesce((SELECT (valeur #>> '{}')::int FROM parametre WHERE cle = 'notification_retention_mois'), 6) AS m",
        (),
        role=role,
    )
    mois = max(1, int((retention or {}).get("m") or 6))
    purged = db.fetch_one(
        "WITH del AS (DELETE FROM notification WHERE cree_le < now() - make_interval(months => %s) RETURNING 1) "
        "SELECT count(*) AS n FROM del",
        (mois,),
        role=role,
    )
    result["notifications_purgees"] = int((purged or {}).get("n") or 0)

    # 7) Data-retention renewal: yearly consent notice + automatic window renewal.
    from .retention import scan_renouvellement

    result["retention_renouvellements"] = scan_renouvellement(role)

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
def declencher_quotidien(user: Annotated[UserMe, Depends(require_permission("notifications.gerer"))]) -> dict[str, object]:
    """Run the daily job on demand (admin), for testing."""
    result = _run_quotidien(role=user.role)
    audit.log(user.id, user.role, "declenchement_notifications", "membre", None, result)
    return result


# --- Admin: notification catalogue ------------------------------------------

class TypeToggle(BaseModel):
    actif: bool


@router.get("/admin/notifications/types")
def list_types(user: Annotated[UserMe, Depends(require_permission("notifications.consulter"))]) -> list[dict[str, object]]:
    rows = db.fetch_all("SELECT cle, libelle, categorie, actif, scheduled, sensibilite FROM type_notification ORDER BY categorie, libelle", (), role=user.role)
    return [
        {"cle": r["cle"], "libelle": r["libelle"], "categorie": r["categorie"], "actif": bool(r["actif"]),
         "scheduled": bool(r["scheduled"]), "sensibilite": r.get("sensibilite") or "operationnel"}
        for r in rows
    ]


@router.put("/admin/notifications/types/{cle}")
def toggle_type(cle: str, payload: TypeToggle, user: Annotated[UserMe, Depends(require_permission("notifications.gerer"))]) -> dict[str, object]:
    current = db.fetch_one("SELECT sensibilite FROM type_notification WHERE cle = %s", (cle,), role=user.role)
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown type")
    # A critical security type (unusual login, security alert, expired code) is
    # always delivered by the engine; letting an admin "disable" it would be a lie.
    if (current.get("sensibilite") or "") == "critique" and not payload.actif:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un type critique de sécurité ne peut pas être désactivé.",
        )
    row = db.execute("UPDATE type_notification SET actif = %s, maj_le = now() WHERE cle = %s RETURNING cle", (payload.actif, cle), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown type")
    audit.log(user.id, user.role, "toggle_type_notification", "type_notification", cle, {"actif": payload.actif})
    return {"ok": True, "actif": payload.actif}


@router.get("/admin/notifications/echecs")
def list_echecs(
    user: Annotated[UserMe, Depends(require_permission("notifications.consulter"))], limit: int = 100, inclure_resolus: bool = False
) -> dict[str, object]:
    """Delivery-failure observability: the recent failed channel sends so the
    administration can see who did not receive a message and follow up."""
    where = "" if inclure_resolus else "WHERE NOT e.resolu"
    rows = db.fetch_all(
        "SELECT e.id, e.membre_id, e.type_cle, e.canal, e.detail, e.resolu, e.cree_le, "
        "trim(coalesce(m.prenoms, '') || ' ' || coalesce(m.nom, '')) AS membre "
        "FROM notification_echec e LEFT JOIN membre m ON m.id = e.membre_id "
        f"{where} ORDER BY e.cree_le DESC LIMIT %s",
        (max(1, min(limit, 500)),),
        role=user.role,
    )
    ouverts = db.fetch_one("SELECT count(*) AS n FROM notification_echec WHERE NOT resolu", (), role=user.role) or {"n": 0}
    return {
        "ouverts": int(ouverts.get("n") or 0),
        "echecs": [
            {
                "id": str(r["id"]),
                "membre_id": str(r["membre_id"]) if r.get("membre_id") else None,
                "membre": (str(r.get("membre") or "").strip() or None),
                "type_cle": r["type_cle"],
                "canal": r["canal"],
                "detail": r["detail"],
                "resolu": bool(r["resolu"]),
                "cree_le": r["cree_le"].isoformat() if r["cree_le"] else None,
            }
            for r in rows
        ],
    }


@router.post("/admin/notifications/echecs/{echec_id}/resolu", status_code=status.HTTP_204_NO_CONTENT)
def resoudre_echec(echec_id: str, user: Annotated[UserMe, Depends(require_permission("notifications.gerer"))]) -> None:
    """Mark a delivery failure as handled so it leaves the open list."""
    row = db.execute("UPDATE notification_echec SET resolu = true WHERE id = %s RETURNING id", (echec_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown failure")
    audit.log(user.id, user.role, "resoudre_echec_notification", "notification_echec", echec_id, {})


# --- Member: language -------------------------------------------------------

class LangueIn(BaseModel):
    langue: str


@router.put("/membres/me/langue")
def set_langue(payload: LangueIn, user: Annotated[UserMe, Depends(require_permission("membres.self"))]) -> dict[str, object]:
    if payload.langue not in ("fr", "en"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="langue must be fr or en")
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    db.execute("UPDATE membre SET langue = %s WHERE id = %s", (payload.langue, user.membre_id), role=user.role)
    return {"ok": True, "langue": payload.langue}


class ThemeIn(BaseModel):
    theme: str


@router.put("/membres/me/theme")
def set_theme(payload: ThemeIn, user: Annotated[UserMe, Depends(require_permission("membres.self"))]) -> dict[str, object]:
    """The member's display theme: light, dark or system (follow the device)."""
    if payload.theme not in ("light", "dark", "system"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="theme must be light, dark or system")
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    db.execute("UPDATE membre SET theme = %s WHERE id = %s", (payload.theme, user.membre_id), role=user.role)
    return {"ok": True, "theme": payload.theme}
