"""Attendance survey ("sondage de pointage") of an activity: automatic and manual send.

Each recipient gets the activity start localized to THEIR own time zone plus the
lightweight public check-in link, through the existing notification pipeline (in-app,
Telegram and e-mail, honouring the admin toggle, member preferences and language). The
link lands on the single ``participation`` source of truth, so the survey stays
single-participation and anti-duplicate across every channel.

Automatic dispatch: a frequent scheduler (``GET /cron/sondages``) sends the survey once
per activity when it reaches ``debut + offset`` (offset configurable by the
administration, negative = before start). A per-event marker
(``sondage_pointage_envoye_le``) makes it idempotent. The administration can also send
manually at any time from the activity, as a fallback that never breaks the automatic
path.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, channels, db, notifications, temps
from .cron_auth import require_cron_auth
from .permissions_rbac import require_permission
from .schemas import UserMe
from .visibilite import CIBLE_PREDICATE

router = APIRouter(prefix="/api/v1/admin", tags=["sondage"])
cron_router = APIRouter(prefix="/api/v1", tags=["sondage"])


class SondageIn(BaseModel):
    """Optional explicit recipients; when omitted, the whole event audience is surveyed."""

    membre_ids: list[str] | None = None


class SondageConfigIn(BaseModel):
    auto: bool
    offset_min: int = Field(ge=-720, le=720)  # up to 12h before/after the start


def _base_pointage() -> str:
    return channels.integration_value("pointage_externe") or "https://adsum-public.pages.dev"


def _config(role: str | None) -> tuple[bool, int]:
    """(automatic survey enabled, offset in minutes relative to the activity start)."""
    auto = db.fetch_one("SELECT (valeur #>> '{}') AS v FROM parametre WHERE cle = 'sondage_pointage_auto'", (), role=role)
    off = db.fetch_one("SELECT (valeur #>> '{}')::int AS v FROM parametre WHERE cle = 'sondage_pointage_offset_min'", (), role=role)
    auto_on = (auto is None) or (str(auto.get("v")) == "true")  # default ON
    # Default trigger offset is 30 minutes after the scheduled start: the survey
    # window opens at start + 30 min for the time-scan fallback. The primary path is
    # event-driven (sent when the admin opens the live session), which ignores the
    # offset. Admin-configurable via set_config_sondage.
    offset = int(off["v"]) if off and off.get("v") is not None else 30
    return auto_on, offset


def _recipients(evenement_id: str, role: str | None, membre_ids: list[str] | None) -> list[dict[str, object]]:
    if membre_ids:
        return db.fetch_all(
            "SELECT id, prenoms, fuseau_horaire FROM membre WHERE id = ANY(%s::uuid[]) AND statut = 'actif'",
            (membre_ids,),
            role=role,
        )
    # Default reach is the event AUDIENCE (CIBLE_PREDICATE), never the whole base, so a
    # restricted activity's survey never leaks its title and link to every member.
    return db.fetch_all(
        "SELECT m.id, m.prenoms, m.fuseau_horaire FROM membre m "
        "LEFT JOIN intendance mi ON mi.id = m.intendance_id "
        "JOIN evenement e ON e.id = %s "
        f"WHERE m.statut = 'actif' AND {CIBLE_PREDICATE}",
        (evenement_id,),
        role=role,
    )


def dispatcher_sondage(
    ev: dict[str, object], role: str | None, *, membre_ids: list[str] | None = None, dedup: bool = False
) -> dict[str, object]:
    """Send the attendance survey of one activity to its audience (or an explicit list),
    localized to each member's zone. Enables the public check-in link on the activity so
    the link actually resolves. Returns delivery counts. Never raises on a per-member
    channel failure (that is recorded by the notification pipeline)."""
    evenement_id = str(ev["id"])
    # The survey relies on the public check-in link (/?e=<id>); enable external
    # check-in so the link resolves for the surveyed activity.
    if not ev.get("emargement_externe"):
        db.execute("UPDATE evenement SET emargement_externe = true WHERE id = %s", (evenement_id,), role=role)
    lien = f"{_base_pointage()}/?e={evenement_id}"
    rows = _recipients(evenement_id, role, membre_ids)
    envoyes = 0
    canaux: set[str] = set()
    sans_canal = 0
    for m in rows:
        quand = temps.formater_instant(ev["debut"], m.get("fuseau_horaire"))
        ctx = {"prenom": m.get("prenoms") or "", "titre": ev["titre"], "quand": quand, "lien": lien}
        used = notifications.notifier(str(m["id"]), role, "sondage_activite", ctx, ref_id=evenement_id, dedup=dedup)
        if used:
            envoyes += 1
            canaux.update(used)
        else:
            sans_canal += 1
    return {"cibles": len(rows), "envoyes": envoyes, "canaux": sorted(canaux), "sans_canal": sans_canal, "lien": lien}


def envoyer_sondage_ouverture(evenement_id: str, role: str | None) -> dict[str, object]:
    """Send the attendance survey ONCE, when the activity's live session is opened.

    Event-driven (called from the session-open action), so the survey reaches the
    audience exactly when the activity is launched, and never again: the per-event
    marker ``sondage_pointage_envoye_le`` makes the send idempotent, so re-opening
    or toggling the session does not re-notify anyone. Respects the admin toggle
    (auto on/off) and never raises (a survey must not break launching a session)."""
    try:
        auto_on, _ = _config(role)
        if not auto_on:
            return {"actif": False, "envois": 0}
        ev = db.fetch_one(
            "SELECT id, titre, debut, emargement_externe FROM evenement "
            "WHERE id = %s AND coalesce(annule, false) = false AND sondage_pointage_envoye_le IS NULL",
            (evenement_id,),
            role=role,
        )
        if not ev:
            return {"actif": True, "envois": 0, "deja_envoye": True}
        # Mark first (idempotent guard), so two concurrent opens never double send.
        marque = db.execute(
            "UPDATE evenement SET sondage_pointage_envoye_le = now() "
            "WHERE id = %s AND sondage_pointage_envoye_le IS NULL RETURNING id",
            (evenement_id,),
            role=role,
        )
        if not marque:
            return {"actif": True, "envois": 0, "deja_envoye": True}
        res = dispatcher_sondage(ev, role, dedup=True)
        audit.log(None, role, "envoi_sondage", "evenement", evenement_id,
                  {"cibles": res["cibles"], "envoyes": res["envoyes"], "mode": "ouverture_session"})
        return {"actif": True, "envois": int(res["envoyes"]), "cibles": res["cibles"]}
    except Exception:  # noqa: BLE001 - the survey must never block opening the session
        return {"actif": True, "envois": 0, "erreur": True}


@router.post("/evenements/{evenement_id}/sondage")
def envoyer_sondage(
    evenement_id: str,
    payload: SondageIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.superviser"))],
) -> dict[str, object]:
    """Manually send the attendance survey of an activity (fallback / re-send). Works
    for any activity: it enables the public check-in link if needed."""
    ev = db.fetch_one("SELECT id, titre, debut, emargement_externe, annule FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="activité introuvable")
    if ev.get("annule"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="activité annulée : réactivez-la avant d'envoyer le sondage")
    res = dispatcher_sondage(ev, user.role, membre_ids=payload.membre_ids, dedup=False)
    audit.log(user.id, user.role, "envoi_sondage", "evenement", evenement_id,
              {"cibles": res["cibles"], "envoyes": res["envoyes"], "cible": "liste" if payload.membre_ids else "audience", "mode": "manuel"})
    return res


def dispatcher_sondages_dus(role: str | None) -> dict[str, object]:
    """Send the survey of every activity that just reached its trigger moment
    (``debut + offset``) and has not been dispatched yet. Idempotent through the
    per-event marker; a two-hour grace avoids firing for old activities when the
    feature is turned on. Returns how many activities were processed."""
    auto_on, offset = _config(role)
    if not auto_on:
        return {"actif": False, "traites": 0, "envois": 0}
    dus = db.fetch_all(
        """
        SELECT id, titre, debut, emargement_externe
        FROM evenement e
        WHERE e.debut IS NOT NULL
          AND coalesce(e.annule, false) = false
          AND e.sondage_pointage_envoye_le IS NULL
          AND now() >= e.debut + make_interval(mins => %(off)s)
          AND now() <= e.debut + make_interval(mins => %(off)s) + interval '2 hours'
        ORDER BY e.debut ASC
        """,
        {"off": offset},
        role=role,
    )
    traites = 0
    envois = 0
    for ev in dus:
        # Mark first (idempotent guard) so a concurrent run never double sends, then
        # dispatch with per-member dedup as a second safety net.
        marque = db.execute(
            "UPDATE evenement SET sondage_pointage_envoye_le = now() WHERE id = %s AND sondage_pointage_envoye_le IS NULL RETURNING id",
            (str(ev["id"]),),
            role=role,
        )
        if not marque:
            continue
        res = dispatcher_sondage(ev, role, dedup=True)
        traites += 1
        envois += int(res["envoyes"])
        audit.log(None, role, "envoi_sondage", "evenement", str(ev["id"]),
                  {"cibles": res["cibles"], "envoyes": res["envoyes"], "mode": "auto", "offset_min": offset})
    return {"actif": True, "traites": traites, "envois": envois, "offset_min": offset}


@cron_router.get("/cron/sondages")
def cron_sondages(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    """Frequent scheduled job (every few minutes): dispatch the attendance survey of
    activities reaching their trigger moment. Secured by the CRON_SECRET bearer."""
    require_cron_auth(authorization)
    return dispatcher_sondages_dus(role=None)


@router.get("/parametres/sondage-pointage")
def get_config_sondage(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, object]:
    auto_on, offset = _config(user.role)
    return {"auto": auto_on, "offset_min": offset}


@router.put("/parametres/sondage-pointage")
def set_config_sondage(payload: SondageConfigIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, object]:
    db.execute(
        "INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le) "
        "VALUES ('sondage_pointage_auto', %s::jsonb, 'sondage', 'Envoi automatique du sondage de pointage', %s, now()) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
        ("true" if payload.auto else "false", user.id),
        role=user.role,
    )
    db.execute(
        "INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le) "
        "VALUES ('sondage_pointage_offset_min', %s::jsonb, 'sondage', 'Minutes relatives au debut pour declencher le sondage', %s, now()) "
        "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()",
        (str(int(payload.offset_min)), user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "config_sondage_pointage", "parametre", "sondage_pointage", {"auto": payload.auto, "offset_min": payload.offset_min})
    return {"auto": payload.auto, "offset_min": payload.offset_min}


@router.post("/notifications/declencher-sondages")
def declencher_sondages(user: Annotated[UserMe, Depends(require_permission("notifications.gerer"))]) -> dict[str, object]:
    """Run the automatic survey scan on demand (admin test), without breaking the cron."""
    res = dispatcher_sondages_dus(role=user.role)
    audit.log(user.id, user.role, "declenchement_sondages", "evenement", None, res)
    return res
