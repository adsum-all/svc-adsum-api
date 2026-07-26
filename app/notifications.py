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
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from . import audit, channels, db, fonctions_membre, identite, temps
from .config import settings
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
    "date_reference_rappel": "rappels",
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
    # Collaboration (spaces): gated by the member "collaboration" preference so a
    # member can mute collaboration noise (operational sensitivity).
    "collab_mention": "collaboration",
    "collab_assignation": "collaboration",
    "collab_echeance": "collaboration",
    "collab_publication": "collaboration",
    "collab_demande": "collaboration",
}


# Member-facing notification GROUPS. Each group can be turned on/off independently
# PER CHANNEL (e-mail and Telegram) by the member, via the matrice_canaux JSONB.
# Types not listed here are ungrouped: either critical/mandatory (always delivered)
# or essential account messages that follow the master channel switches only.
_GROUPE = {
    "activite_rappel_j1": "rappels",
    "agenda_hebdo": "rappels",
    "attestation_rappel": "rappels",
    "participation_bientot_close": "rappels",
    "activite_demarree": "demarrage",
    "activite_rappel_start": "demarrage",
    "activite_modifiee": "changements",
    "activite_annulee": "changements",
    "questionnaire_disponible": "changements",
    "activite_test_diffusion": "changements",
    "recap_hebdo": "recap",
    "anniversaire": "anniversaires",
    "anniversaire_pairs": "anniversaires",
    "collab_mention": "collaboration",
    "collab_assignation": "collaboration",
    "collab_echeance": "collaboration",
    "collab_publication": "collaboration",
    "collab_demande": "collaboration",
    "demande_reponse": "dossier",
    "modification_decision": "dossier",
    "document_demande": "dossier",
    # Attendance survey (pointage) and admin announcements are now member-controllable per
    # channel (the member gets on/off buttons), instead of bypassing the matrix. Their
    # channel defaults stay ON (see _matrice_defaut) so no one silently loses them.
    "sondage_activite": "pointage",
    "annonce": "annonces",
    # modification_complement (a complement REQUESTED from the member, action needed)
    # stays ungrouped / always sent, so it is never muted on every push channel.
}
# Ordered list of groups (also the source of truth for the settings UI and defaults).
GROUPES = ("dossier", "collaboration", "changements", "demarrage", "rappels", "recap", "anniversaires", "pointage", "annonces")
# Default matrix: Telegram everywhere, e-mail on everywhere (no member loses an
# e-mail by surprise); each member then refines. Kept in one place.
def _matrice_defaut() -> dict[str, dict[str, bool]]:
    return {g: {"email": True, "telegram": True} for g in GROUPES}


# Backward-compatibility: when a member has no matrix yet (matrice_canaux IS NULL),
# fall back to the legacy per-category boolean they may have set in the old UI, so an
# explicit opt-out is preserved. Maps a new group to the old preference column.
_GROUPE_LEGACY_COL = {
    "rappels": "rappels",
    "recap": "rappels",
    "demarrage": "evenements",
    "changements": "evenements",
    "anniversaires": "anniversaire",
    "collaboration": "collaboration",
    "dossier": "demandes",
}
_MATRICE_COLS = (
    "matrice_canaux", "email", "telegram", "whatsapp", "sms",
    "evenements", "demandes", "rappels", "anniversaire", "anniv_pairs", "collaboration",
)


def canaux_autorises(membre_id: str, role: str | None, type_cle: str, sensibilite: str) -> set[str] | None:
    """The channels a member allows for one notification type.

    Returns None for a critical or ungrouped type (dispatch then applies only the
    master channel switches / critical bypass). Otherwise returns the set of push
    channels allowed for the type's group: e-mail and Telegram from the per-group
    matrix (master switch AND group switch), WhatsApp/SMS from the master switch as
    before. In-app is always delivered by dispatch regardless, so the member never
    misses information even when a group is muted on every push channel."""
    if sensibilite == "critique":
        return None
    groupe = _GROUPE.get(type_cle)
    if groupe is None:
        return None
    try:
        prefs = db.fetch_one(
            f"SELECT {', '.join(_MATRICE_COLS)} FROM preference_notification WHERE membre_id = %s",
            (membre_id,), role=role,
        ) or {}
    except Exception:  # noqa: BLE001 - preference lookup must never break a send
        return None
    matrice = prefs.get("matrice_canaux")
    grp = matrice.get(groupe) if isinstance(matrice, dict) else None
    # Legacy fallback uses the exact old per-TYPE category column (so a member's
    # explicit opt-out, e.g. anniv_pairs, is preserved when they have no matrix yet).
    legacy_col = _CATEGORY_PREF.get(type_cle)
    legacy_on = bool(prefs.get(legacy_col, True)) if legacy_col else True
    allowed: set[str] = set()
    for canal in ("email", "telegram"):
        master_on = bool(prefs.get(canal, True))
        group_on = bool(grp.get(canal, True)) if isinstance(grp, dict) else legacy_on
        if master_on and group_on:
            allowed.add(canal)
    for canal in ("whatsapp", "sms"):
        master_on = bool(prefs.get(canal, False))
        group_on = bool(grp.get(canal, True)) if isinstance(grp, dict) else legacy_on
        if master_on and group_on:
            allowed.add(canal)
    return allowed


def _whatsapp_template_for(type_cle: str) -> str | None:
    """The approved WhatsApp template for a notification type, or None when none is
    configured (WhatsApp is then skipped for that type, other channels still send)."""
    if type_cle == "anniversaire":
        return settings.whatsapp_template_anniversaire or None
    if _CATEGORY_PREF.get(type_cle) == "collaboration":
        return settings.whatsapp_template_collab or None
    return None

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


def _gate_eligibilite(membre_id: str, type_cle: str, role: str | None) -> bool:
    """Decide whether this member may receive this kind of message, and record why not.

    Kept apart from :func:`notifier` so the gate stays readable, and so its own failure
    can never be mistaken for a refusal: if the ledger write fails, the message still
    goes out. Blocking someone because a log line could not be written would turn an
    observability problem into a delivery outage.
    """
    from . import eligibilite

    try:
        permis, motif = eligibilite.autorise(membre_id, type_cle, role)
    except Exception:  # noqa: BLE001 - never let the gate itself silence a message
        return True
    if permis:
        return True
    try:
        db.execute(
            "INSERT INTO notification_echec (membre_id, type_cle, canal, detail) VALUES (%s, %s, %s, %s)",
            (membre_id, type_cle, "eligibilite", f"notification bloquée : {motif}"),
            role=role,
        )
    except Exception:  # noqa: BLE001 - the refusal stands even if it could not be logged
        pass
    return False


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
        # THE eligibility gate. It sits here, in the single funnel every notification
        # goes through, precisely so no caller can forget it: recipient-selection
        # queries filtered on membre.statut alone, which is 'actif' from the moment an
        # administrator registers someone, so people were invited to confirm their
        # attendance at activities before they could even log in. A refusal is recorded
        # with its reason, because a message that silently vanishes is
        # indistinguishable from a bug.
        if not _gate_eligibilite(membre_id, type_cle, role):
            return []

        active = db.fetch_one("SELECT actif, sensibilite FROM type_notification WHERE cle = %s", (type_cle,), role=role)
        sensibilite = (active or {}).get("sensibilite") or "operationnel"
        critique = sensibilite == "critique"
        # A critical security message is never silenced by an admin toggle; other
        # types can be turned off in the catalogue.
        if active and not active["actif"] and not critique:
            return []
        member = db.fetch_one("SELECT langue FROM membre WHERE id = %s", (membre_id,), role=role)
        lang = (member["langue"] if member and member.get("langue") else "fr") or "fr"
        # Personal address: greet the member by their honorific title / name. The member's
        # identity is authoritative, so appellation keys override any name the caller passed;
        # the caller keeps its own context keys (titre, date, lien, liste, motif...).
        ctx = {**ctx, **_appellation(membre_id, role)}
        # Per-group, per-channel matrix: for a gateable type, compute which push
        # channels this member allows (e-mail / Telegram / WhatsApp / SMS). None means
        # "not gated here" (critical or ungrouped): dispatch then applies only the
        # master switches. In-app is always delivered, so muting every push channel
        # never hides the information, it just stops the e-mail/Telegram push.
        autorises = canaux_autorises(membre_id, role, type_cle, sensibilite)
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
        # Plain-text footer for Telegram / in-app: the admin-configured signature then site.
        footer = "\n\n" + signature + (f"\n{site}" if site else "")
        corps_text = corps + (footer if footer.strip() else "")
        if type_cle == "anniversaire":
            html = render_anniversaire_email(titre, corps, site=site or None, signature=signature)
        else:
            html = render_notification_email(titre, corps, signature=signature, site=site or None)
        msg = channels.Message(
            titre=titre, corps_text=corps_text, corps_html=html, type_notif=type_cle,
            whatsapp_template=_whatsapp_template_for(type_cle),
        )
        # Critical messages ignore the channel kill-switch and the member's channel
        # preferences: security must always be attempted on every reachable channel.
        used = channels.dispatch(
            membre_id, role, msg, whatsapp_params=whatsapp_params, critique=critique, canaux_autorises=autorises
        )
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
    liste = identite.liste_prenoms(row.get("prenoms"))  # normalised casing ("jean" -> "Jean")
    return liste[0] if liste else "cher membre"


def _appellation(membre_id: str, role: str | None) -> dict[str, str]:
    """How to address a member in a message: honorific title when they hold a confirmed
    function (Pasteur Jean...), the pastoral appellation for a Berger/Bergere, else the
    first name in correct casing. Returns keys merged into the template context so every
    notification greets the member personally. Fully defensive: any failure falls back to a
    neutral greeting so a notification is never dropped over personalization."""
    try:
        m = db.fetch_one(
            "SELECT prenoms, nom, nom_naissance, nom_marital, nom_affiche, genre, est_berger, nom_pastoral "
            "FROM membre WHERE id = %s",
            (membre_id,), role=role,
        ) or {}
        liste = identite.liste_prenoms(m.get("prenoms"))
        prenom = liste[0] if liste else "cher membre"
        choix = m.get("nom_affiche")
        fam = m.get("nom_naissance") if choix == "naissance" and m.get("nom_naissance") else (
            m.get("nom_marital") if choix == "marital" and m.get("nom_marital") else m.get("nom"))
        nom_civil = identite.nom_affichage(fam, m.get("prenoms"))
        # Central resolver: every confirmed function (with its category) feeds the
        # single precedence rule (special function > title > function > particular),
        # so a Moderateur who is also a Berger is greeted "Moderateur (Berger X)".
        fonctions = fonctions_membre.fonctions_publiques(membre_id, m.get("genre"), role)
        ident = identite.resoudre_identite(
            genre=m.get("genre"), prenoms=m.get("prenoms"), nom_civil=nom_civil,
            est_berger=bool(m.get("est_berger")), nom_pastoral=m.get("nom_pastoral"),
            fonctions=fonctions,
        )
        appellation = str(ident.get("appellation") or prenom)
        # ``prenom`` carries the appellation so existing "Bonjour {prenom}" templates greet
        # with the title without any template change; ``prenom_simple`` keeps the bare name.
        return {"prenom": appellation, "prenom_simple": prenom, "appellation": appellation, "nom": nom_civil, "salutation": f"Bonjour {appellation}"}
    except Exception:  # noqa: BLE001 - personalization must never break a notification
        return {"prenom": "cher membre", "appellation": "cher membre", "salutation": "Bonjour"}


# --- Weekly digest scheduling (per member, in the member's own timezone) -----

# Default local hour at which the weekly recap + agenda are delivered on the week's
# first day. Admin-configurable via the parametre ``hebdo_heure_envoi``.
_HEURE_HEBDO = 8


def _heure_hebdo(role: str | None) -> int:
    try:
        row = db.fetch_one("SELECT (valeur #>> '{}')::int AS h FROM parametre WHERE cle = 'hebdo_heure_envoi'", (), role=role)
        h = int(row["h"]) if row and row.get("h") is not None else _HEURE_HEBDO
        return h if 0 <= h <= 23 else _HEURE_HEBDO
    except Exception:  # noqa: BLE001 - a bad parameter must never break the cron
        return _HEURE_HEBDO


def _semaine_jour_debut(role: str | None) -> int:
    """First day of the week for this organisation (0=Monday ... 6=Sunday), admin-set.
    Falls back to Monday so the platform is never tied to one organisation's calendar."""
    try:
        row = db.fetch_one("SELECT (valeur #>> '{}')::int AS j FROM parametre WHERE cle = 'semaine_jour_debut'", (), role=role)
        j = int(row["j"]) if row and row.get("j") is not None else 0
        return j if 0 <= j <= 6 else 0
    except Exception:  # noqa: BLE001 - a bad parameter must never break the cron
        return 0


def _debut_semaine_locale(local: datetime, jour_debut: int) -> datetime:
    """Midnight of the current local week's first day, for a member's local now."""
    recul = (local.weekday() - jour_debut) % 7
    return (local - timedelta(days=recul)).replace(hour=0, minute=0, second=0, microsecond=0)


def _fmt_evt(e: dict[str, object], fuseau: str | None, avec_heure: bool) -> str:
    """One event line, its time shown in the RECIPIENT member's timezone."""
    if not e.get("debut"):
        return str(e.get("titre") or "")
    loc = temps.local_datetime(e["debut"], fuseau)  # type: ignore[arg-type]
    return f"{e['titre']} ({loc.strftime('%d/%m %Hh%M')})" if avec_heure else str(e.get("titre") or "")


def _hebdo_par_membre(now: datetime, role: str | None, actifs: list[dict[str, object]], lien_app: str, result: dict[str, object]) -> None:
    """Send the weekly recap (previous week) and agenda (current week) to each opted-in
    member on the week's first day at 08:00 IN THAT MEMBER'S timezone. Exactly once per
    calendar week (dedup by the member's local week key), so an hourly cron delivers close to
    08:00 local while a daily cron still delivers once, on/after the local Monday 08:00. Week
    bounds follow the admin-configured first day of the week (multi-organisation)."""
    jour_debut = _semaine_jour_debut(role)
    # Fetch community-wide events once, over a window wide enough to cover the previous and
    # current week for every timezone, then slice per member locally (no per-member query).
    evenements = db.fetch_all(
        "SELECT titre, debut, fuseau_horaire FROM evenement WHERE cible_type = 'general' "
        "AND debut BETWEEN now() - interval '9 days' AND now() + interval '9 days' ORDER BY debut ASC",
        (),
        role=role,
    )
    heure_envoi = _heure_hebdo(role)
    for m in actifs:
        fuseau = m.get("fuseau_horaire")
        local = temps.local_datetime(now, fuseau)  # type: ignore[arg-type]
        debut_cette = _debut_semaine_locale(local, jour_debut)
        cible = debut_cette.replace(hour=heure_envoi)
        if local < cible:
            continue  # the member's local send hour has not arrived yet this week
        fin_cette = debut_cette + timedelta(days=7)
        debut_prec = debut_cette - timedelta(days=7)
        semaine_key = debut_cette.strftime("%Y-%m-%d")
        cette = [e for e in evenements if e.get("debut") and debut_cette <= temps.local_datetime(e["debut"], fuseau) < fin_cette]  # type: ignore[arg-type]
        precedente = [e for e in evenements if e.get("debut") and debut_prec <= temps.local_datetime(e["debut"], fuseau) < debut_cette]  # type: ignore[arg-type]
        prenom = _prenom(m)
        # Recap of the previous week (titles only). Opt-in via the "recap" group matrix.
        if precedente:
            liste = "; ".join(_fmt_evt(e, fuseau, False) for e in precedente)
            if notifier(str(m["id"]), role, "recap_hebdo", {"prenom": prenom, "liste": liste}, ref_id=f"{semaine_key}-recap", dedup=True):
                result["recap"] += 1
        # Agenda of the current week (with local times). Opt-in via the "rappels" group.
        if cette:
            liste = "; ".join(_fmt_evt(e, fuseau, True) for e in cette)
            if notifier(str(m["id"]), role, "agenda_hebdo", {"prenom": prenom, "liste": liste, "lien": lien_app}, ref_id=semaine_key, dedup=True):
                result["agenda"] += 1


# --- Daily scheduled job ----------------------------------------------------

def _run_quotidien(role: str | None) -> dict[str, object]:
    now = datetime.now(tz=UTC)
    annee = now.year
    result = {"anniversaires": 0, "rappels_j1": 0, "agenda": 0, "recap": 0, "digest_pairs": 0, "participation_close": 0}

    # 1) Birthdays today. Cross-path idempotence: the manual admin trigger marks its
    # sends in notification_anniversaire; skip those members so running both paths the
    # same day never produces a second wish (each path keeps its own dedup ledger).
    for r in db.fetch_all(
        "SELECT id, prenoms FROM membre WHERE date_naissance IS NOT NULL AND statut = 'actif' "
        "AND extract(month from date_naissance) = extract(month from now()) "
        "AND extract(day from date_naissance) = extract(day from now()) "
        "AND NOT EXISTS (SELECT 1 FROM notification_anniversaire na "
        "WHERE na.membre_id = membre.id AND na.annee = extract(year from now())::int)",
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
        "SELECT id, titre, debut, fuseau_horaire FROM evenement WHERE debut IS NOT NULL "
        "AND debut BETWEEN now() + interval '12 hours' AND now() + interval '36 hours'",
        (),
        role=role,
    )
    actifs = db.fetch_all("SELECT id, prenoms, langue, fuseau_horaire FROM membre WHERE statut = 'actif'", (), role=role)
    lien_app = channels.integration_value("site_officiel") or "https://adsum-web-membre.pages.dev"
    from .visibilite import CIBLE_PREDICATE

    for ev in evs:
        # Only the members TARGETED by this event receive the reminder: a restricted
        # event (commission/intendance/coordination/tribu) never leaks its title and
        # time to the whole base. General events still reach everyone.
        cibles = db.fetch_all(
            "SELECT m.id, m.prenoms, m.langue, m.fuseau_horaire FROM membre m "
            "LEFT JOIN intendance mi ON mi.id = m.intendance_id "
            "JOIN evenement e ON e.id = %s "
            f"WHERE m.statut = 'actif' AND {CIBLE_PREDICATE}",
            (str(ev["id"]),),
            role=role,
        )
        for m in cibles:
            # Format the date/time in the RECIPIENT's own zone (never raw UTC), so a
            # member is never told the wrong day, consistent with the attendance survey.
            local = temps.local_datetime(ev["debut"], m.get("fuseau_horaire")) if ev["debut"] else None
            date_str = local.strftime("%d/%m/%Y") if local else ""
            heure_str = local.strftime("%Hh%M") if local else ""
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

    # 3+4) Weekly recap (previous week) + agenda (current week), delivered on the week's
    # first day at 08:00 in EACH member's own timezone, exactly once per calendar week.
    _hebdo_par_membre(now, role, actifs, lien_app, result)

    # 5) Manual-attestation follow-up: strategic reminders and expiry.
    result["attest_rappels"], result["attest_expirees"] = _scan_attestations(role)

    # 6) Housekeeping: delete Telegram messages past the retention window.
    purge = channels.purge_old_telegram(role)
    result["telegram_purges"] = purge.get("supprimes", 0)

    # 6a-bis) Moderator channel: purge voice-note audio past the 30-day retention
    # window (unless the message opted to keep its audio). Transcriptions are kept.
    from .collaboration_canal import purge_audio_expire

    result["canal_audio_purges"] = purge_audio_expire(role)

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
        # Re-lock only when the member has no OTHER request still waiting on them,
        # so auto-closing one late cycle never breaks a second legitimate open cycle.
        db.execute(
            "UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s "
            "AND NOT EXISTS (SELECT 1 FROM demande d2 WHERE d2.membre_id = membre.id "
            "AND d2.id <> %s AND d2.statut IN ('attente_membre', 'pieces_demandees'))",
            (str(d["membre_id"]), str(d["id"])),
            role=role,
        )
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

    # 7bis) Communications retention: archive old Informations and read/old
    # notifications, cleanup Telegram within the Bot API window. Deletion stays OFF
    # unless the administrator opted in (retention_auto_suppression). Never breaks
    # the daily run.
    try:
        from .retention_archivage import executer_retention

        rapport = executer_retention(simulation=False, acteur="cron:quotidien", role=role)
        result["retention_communications"] = rapport
    except Exception as exc:  # noqa: BLE001 - retention must never break the daily cron
        result["retention_communications_erreur"] = str(exc)[:200]

    # 8) Collaboration card due-date reminders. The card carries a reminder offset
    # (2j / 1j / day-of); on the matching day, its assignees and followers who map
    # to a member get a real reminder (in-app + channels), deduped per card+day.
    result["collab_echeances"] = 0
    for c in db.fetch_all(
        "SELECT c.id, c.titre, c.echeance FROM collab_carte c "
        "WHERE c.echeance IS NOT NULL AND NOT c.archive AND coalesce(c.rappel, 'aucun') <> 'aucun' "
        "AND c.echeance::date = CASE c.rappel WHEN '2j' THEN (now() + interval '2 days')::date "
        "WHEN '1j' THEN (now() + interval '1 day')::date ELSE now()::date END",
        (),
        role=role,
    ):
        ech = c["echeance"].strftime("%d/%m/%Y") if c["echeance"] else ""
        jalon = c["echeance"].strftime("%Y-%m-%d") if c["echeance"] else ""
        for d in db.fetch_all(
            "SELECT DISTINCT u.membre_id, m.prenoms FROM collab_carte_membre cm "
            "JOIN utilisateur u ON u.id = cm.utilisateur_id JOIN membre m ON m.id = u.membre_id "
            "WHERE cm.carte_id = %s AND u.membre_id IS NOT NULL AND m.statut = 'actif'",
            (str(c["id"]),),
            role=role,
        ):
            ctx = {"prenom": _prenom(d), "titre": c["titre"], "echeance": ech}
            if notifier(str(d["membre_id"]), role, "collab_echeance", ctx, ref_id=f"{c['id']}:{jalon}", dedup=True):
                result["collab_echeances"] += 1

    # Attendance survey safety net: the frequent scan (/cron/sondages) is what
    # normally fires the survey at debut+offset, but the Vercel Hobby plan only
    # schedules one daily cron. Running the dispatcher here guarantees a daily
    # automatic pass even without an external sub-hourly scheduler, so an activity
    # whose trigger window overlaps this run is never missed. It stays idempotent
    # (per-event marker) and bounded by the 2h grace window, so no stale survey is
    # sent. A finer external scheduler remains the way to fire close to start time.
    try:
        from .sondage import dispatcher_sondages_dus

        result["sondages"] = int(dispatcher_sondages_dus(role=role).get("traites", 0))
    except Exception:  # noqa: BLE001 - the survey scan must never break the daily job
        result["sondages"] = 0

    # Purge trashed workspaces/boards past the configurable retention window.
    try:
        from .collaboration_corbeille import purger_suppressions

        result["purges"] = purger_suppressions(role)
    except Exception:  # noqa: BLE001 - the purge must never break the daily job
        result["purges"] = {}

    # Reference-date reminders (opt-in): notify members of an upcoming institutional
    # or catholic date when the admin set a reminder on it.
    try:
        from .calendrier_institutionnel import rappels_dates_reference

        result["rappels_dates_reference"] = rappels_dates_reference(role)
    except Exception:  # noqa: BLE001 - a reminder failure must never break the daily job
        result["rappels_dates_reference"] = 0

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


@router.get("/cron/hebdomadaire")
def cron_hebdomadaire(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    """Weekly recap + agenda only, safe to call HOURLY so each member is served at 08:00 in
    their own timezone. Fully idempotent (dedup per member per calendar week), so repeated
    hourly calls never double-send. Point an hourly scheduler here for precise local delivery;
    the daily job also runs it as a fallback (once per week) when no hourly trigger exists."""
    require_cron_auth(authorization)
    now = datetime.now(tz=UTC)
    actifs = db.fetch_all("SELECT id, prenoms, langue, fuseau_horaire FROM membre WHERE statut = 'actif'", (), role=None)
    lien_app = channels.integration_value("site_officiel") or "https://adsum-web-membre.pages.dev"
    result = {"recap": 0, "agenda": 0}
    _hebdo_par_membre(now, None, actifs, lien_app, result)
    return result


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
