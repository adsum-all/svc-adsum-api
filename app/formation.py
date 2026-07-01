"""Training sessions: session links, post-session questionnaires, notification
preferences.

The administration attaches a session link to an event, opens or closes the live
session, builds a short questionnaire and reads the responses. A member joins the
session, then answers the questionnaire while it is open (a configurable window
after the session ends, default six hours) and manages which notifications they
receive. Reads for members are scoped to their own data.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .auth import current_user
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["formation"])

require_event_writer = require_roles("super_admin", "admin", "gestionnaire")
require_staff = require_roles("super_admin", "admin", "gestionnaire", "controleur", "direction")


def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


def _fenetre_heures(role: str) -> int:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = 'questionnaire_fenetre_heures'", (), role=role)
    try:
        return int(row["valeur"]) if row and row["valeur"] is not None else 6
    except (TypeError, ValueError):
        return 6


# --- Admin: session link and live session ----------------------------------

class SessionPatch(BaseModel):
    lien_session: str | None = None
    session_ouverte: bool | None = None
    type_diffusion: str | None = Field(default=None, pattern="^(embed|externe|aucun)$")
    visibilite: str | None = Field(default=None, pattern="^(public|membres|prive)$")


@router.patch("/admin/evenements/{evenement_id}/session")
def maj_session(evenement_id: str, payload: SessionPatch, user: Annotated[UserMe, Depends(require_event_writer)]) -> dict[str, object]:
    """Set the session link, broadcast kind/visibility, and open or close the live session."""
    sets: list[str] = []
    params: list[object] = []
    if payload.lien_session is not None:
        sets.append("lien_session = %s")
        params.append(payload.lien_session or None)
    if payload.type_diffusion is not None:
        sets.append("type_diffusion = %s")
        params.append(payload.type_diffusion)
    if payload.visibilite is not None:
        sets.append("visibilite = %s")
        params.append(payload.visibilite)
    if payload.session_ouverte is not None:
        sets.append("session_ouverte = %s")
        params.append(payload.session_ouverte)
        sets.append("ouvert_le = now()" if payload.session_ouverte else "clos_le = now()")
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="nothing to update")
    params.append(evenement_id)
    row = db.execute(f"UPDATE evenement SET {', '.join(sets)} WHERE id = %s RETURNING id, session_ouverte, lien_session", tuple(params), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    audit.log(user.id, user.role, "maj_session_evenement", "evenement", evenement_id, {"ouverte": payload.session_ouverte})
    # When the live session is opened, notify members that the activity has started.
    if payload.session_ouverte:
        _notifier_session_ouverte(evenement_id, str(row["lien_session"] or ""), user.role)
    return {"id": str(row["id"]), "session_ouverte": bool(row["session_ouverte"]), "lien_session": row["lien_session"]}


@router.post("/admin/evenements/{evenement_id}/test-diffusion")
def test_diffusion(evenement_id: str, user: Annotated[UserMe, Depends(require_event_writer)]) -> dict[str, object]:
    """Send a 'live broadcast test' notification so members can verify the stream view.

    The message clearly flags itself as a test. Not deduplicated: each explicit
    admin trigger is a fresh test send.
    """
    from .notifications import notifier

    ev = db.fetch_one("SELECT titre FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    sent = 0
    for m in db.fetch_all("SELECT id FROM membre WHERE statut = 'actif'", (), role=user.role):
        if notifier(str(m["id"]), user.role, "activite_test_diffusion", {"titre": ev["titre"]}, ref_id=evenement_id, dedup=False):
            sent += 1
    audit.log(user.id, user.role, "test_diffusion", "evenement", evenement_id, {"sent": sent})
    return {"ok": True, "sent": sent}


def _notifier_session_ouverte(evenement_id: str, lien: str, role: str) -> None:
    """Fan out the 'activity started' notification (once, deduplicated)."""
    try:
        from .notifications import notifier

        ev = db.fetch_one("SELECT titre FROM evenement WHERE id = %s", (evenement_id,), role=role)
        titre = ev["titre"] if ev else "l'activite"
        for m in db.fetch_all("SELECT id FROM membre WHERE statut = 'actif'", (), role=role):
            notifier(str(m["id"]), role, "activite_demarree", {"titre": titre, "lien": lien}, ref_id=evenement_id, dedup=True)
    except Exception:  # noqa: BLE001 - notifications must never block the session action
        pass


# --- Admin: questionnaire builder -------------------------------------------

class QuestionIn(BaseModel):
    libelle: str
    type: str = "texte"  # 'texte' | 'choix' | 'note'
    options: list[str] = []


class QuestionnaireIn(BaseModel):
    titre: str = "Questionnaire de session"
    questions: list[QuestionIn]


@router.put("/admin/evenements/{evenement_id}/questionnaire")
def definir_questionnaire(evenement_id: str, payload: QuestionnaireIn, user: Annotated[UserMe, Depends(require_event_writer)]) -> dict[str, object]:
    """Create or replace the questionnaire attached to an event."""
    if not payload.questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="at least one question required")
    q = db.execute(
        """
        INSERT INTO questionnaire (evenement_id, titre) VALUES (%s, %s)
        ON CONFLICT (evenement_id) DO UPDATE SET titre = EXCLUDED.titre, actif = true
        RETURNING id
        """,
        (evenement_id, payload.titre),
        role=user.role,
    )
    if not q:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="questionnaire not created")
    qid = str(q["id"])
    db.execute("DELETE FROM question WHERE questionnaire_id = %s", (qid,), role=user.role)
    for i, question in enumerate(payload.questions):
        if question.type not in ("texte", "choix", "note"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid question type")
        db.execute(
            "INSERT INTO question (questionnaire_id, libelle, type, options, ordre) VALUES (%s, %s, %s, %s::jsonb, %s)",
            (qid, question.libelle, question.type, json.dumps(question.options), i),
            role=user.role,
        )
    audit.log(user.id, user.role, "definir_questionnaire", "evenement", evenement_id, {"questions": len(payload.questions)})
    return {"id": qid, "questions": len(payload.questions)}


def _questions(qid: str, role: str) -> list[dict[str, object]]:
    rows = db.fetch_all("SELECT id, libelle, type, options, ordre FROM question WHERE questionnaire_id = %s ORDER BY ordre ASC", (qid,), role=role)
    return [{"id": str(r["id"]), "libelle": r["libelle"], "type": r["type"], "options": r["options"] or []} for r in rows]


@router.get("/admin/evenements/{evenement_id}/questionnaire")
def get_questionnaire_admin(evenement_id: str, user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, object] | None:
    q = db.fetch_one("SELECT id, titre FROM questionnaire WHERE evenement_id = %s", (evenement_id,), role=user.role)
    if not q:
        return None
    return {"id": str(q["id"]), "titre": q["titre"], "questions": _questions(str(q["id"]), user.role)}


@router.get("/admin/evenements/{evenement_id}/reponses")
def get_reponses(evenement_id: str, user: Annotated[UserMe, Depends(require_staff)]) -> list[dict[str, object]]:
    """All member responses to an event questionnaire."""
    rows = db.fetch_all(
        """
        SELECT r.reponses, r.soumis_le, trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre_nom, m.matricule
        FROM reponse_questionnaire r
        JOIN questionnaire q ON q.id = r.questionnaire_id
        JOIN membre m ON m.id = r.membre_id
        WHERE q.evenement_id = %s
        ORDER BY r.soumis_le DESC
        """,
        (evenement_id,),
        role=user.role,
    )
    return [
        {"membre_nom": r["membre_nom"], "matricule": r["matricule"], "reponses": r["reponses"], "soumis_le": r["soumis_le"].isoformat() if r["soumis_le"] else None}
        for r in rows
    ]


# --- Member: answer the questionnaire ---------------------------------------

@router.get("/membres/me/evenements/{evenement_id}/questionnaire")
def get_questionnaire_membre(evenement_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """The questionnaire for an event, with its availability window and whether
    the member has already answered."""
    membre_id, role = ctx
    ev = db.fetch_one("SELECT fin, session_ouverte FROM evenement WHERE id = %s", (evenement_id,), role=role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    q = db.fetch_one("SELECT id, titre FROM questionnaire WHERE evenement_id = %s AND actif", (evenement_id,), role=role)
    if not q:
        return {"disponible": False, "raison": "aucun_questionnaire"}
    fenetre = _fenetre_heures(role)
    window = db.fetch_one(
        "SELECT (fin IS NOT NULL AND now() BETWEEN fin AND fin + (%s || ' hours')::interval) AS ouvert, fin FROM evenement WHERE id = %s",
        (str(fenetre), evenement_id),
        role=role,
    )
    already = db.fetch_one("SELECT 1 FROM reponse_questionnaire WHERE questionnaire_id = %s AND membre_id = %s", (str(q["id"]), membre_id), role=role)
    disponible = bool(window and window["ouvert"]) and not already
    return {
        "disponible": disponible,
        "deja_repondu": bool(already),
        "fenetre_heures": fenetre,
        "titre": q["titre"],
        "questions": _questions(str(q["id"]), role) if disponible else [],
    }


class ReponseIn(BaseModel):
    reponses: dict[str, str]


@router.post("/membres/me/evenements/{evenement_id}/questionnaire", status_code=status.HTTP_201_CREATED)
def repondre_questionnaire(evenement_id: str, payload: ReponseIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Submit answers, only while the questionnaire window is open and once."""
    membre_id, role = ctx
    q = db.fetch_one("SELECT id FROM questionnaire WHERE evenement_id = %s AND actif", (evenement_id,), role=role)
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no questionnaire")
    fenetre = _fenetre_heures(role)
    window = db.fetch_one(
        "SELECT (fin IS NOT NULL AND now() BETWEEN fin AND fin + (%s || ' hours')::interval) AS ouvert FROM evenement WHERE id = %s",
        (str(fenetre), evenement_id),
        role=role,
    )
    if not window or not window["ouvert"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="questionnaire window closed")
    row = db.execute(
        """
        INSERT INTO reponse_questionnaire (questionnaire_id, membre_id, reponses) VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (questionnaire_id, membre_id) DO NOTHING
        RETURNING id
        """,
        (str(q["id"]), membre_id, json.dumps(payload.reponses)),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already answered")
    return {"ok": True}


# --- Member: notification preferences ---------------------------------------

class PreferencesIn(BaseModel):
    evenements: bool = True
    demandes: bool = True
    rappels: bool = True
    email: bool = True
    telegram: bool = True  # Telegram is the free channel, opt-in by default
    whatsapp: bool = False
    sms: bool = False
    anniversaire: bool = True
    anniv_pairs: bool = True  # receive the daily "today's birthdays" digest
    cal_vip: bool = True  # show VIP birthdays in the calendar by default
    cal_responsables: bool = False  # responsables birthdays off by default
    cal_commission: bool = False  # own-commission birthdays behind a filter


_PREF_COLS = (
    "evenements", "demandes", "rappels", "email", "telegram", "whatsapp", "sms",
    "anniversaire", "anniv_pairs", "cal_vip", "cal_responsables", "cal_commission",
)
_PREF_OFF_DEFAULT = ("whatsapp", "sms", "cal_responsables", "cal_commission")


@router.get("/membres/me/preferences-notification")
def get_preferences(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, bool]:
    membre_id, role = ctx
    row = db.fetch_one(f"SELECT {', '.join(_PREF_COLS)} FROM preference_notification WHERE membre_id = %s", (membre_id,), role=role)
    if not row:
        defaults = {c: True for c in _PREF_COLS}
        defaults.update({c: False for c in _PREF_OFF_DEFAULT})
        return defaults
    return {c: bool(row[c]) for c in _PREF_COLS}


@router.put("/membres/me/preferences-notification")
def set_preferences(payload: PreferencesIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, bool]:
    membre_id, role = ctx
    values = {c: getattr(payload, c) for c in _PREF_COLS}
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in _PREF_COLS)
    db.execute(
        f"""
        INSERT INTO preference_notification (membre_id, {', '.join(_PREF_COLS)}, maj_le)
        VALUES (%s, {', '.join(['%s'] * len(_PREF_COLS))}, now())
        ON CONFLICT (membre_id) DO UPDATE SET {assignments}, maj_le = now()
        """,
        (membre_id, *[values[c] for c in _PREF_COLS]),
        role=role,
    )
    return values


# --- Admin: questionnaire window duration -----------------------------------

class FenetreIn(BaseModel):
    heures: int


@router.get("/admin/parametres/questionnaire-fenetre")
def get_fenetre(user: Annotated[UserMe, Depends(require_staff)]) -> dict[str, int]:
    return {"heures": _fenetre_heures(user.role)}


@router.put("/admin/parametres/questionnaire-fenetre")
def set_fenetre(payload: FenetreIn, user: Annotated[UserMe, Depends(require_event_writer)]) -> dict[str, int]:
    if not 1 <= payload.heures <= 168:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="heures must be between 1 and 168")
    db.execute(
        """
        INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le)
        VALUES ('questionnaire_fenetre_heures', %s::jsonb, 'formation', 'Questionnaire availability window in hours', %s, now())
        ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()
        """,
        (str(payload.heures), user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "config_questionnaire_fenetre", "parametre", "questionnaire_fenetre_heures", {"heures": payload.heures})
    return {"heures": payload.heures}
