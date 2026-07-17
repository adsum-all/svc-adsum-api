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

from . import audit, db, ratelimit, visibilite
from .auth import current_user
from .fields import LineStr, LongTextStr, ShortStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["formation"])



def _membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


def _fenetre_heures(role: str) -> int:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = 'questionnaire_fenetre_heures'", (), role=role)
    try:
        return int(row["valeur"]) if row and row["valeur"] is not None else 5
    except (TypeError, ValueError):
        return 5


# --- Admin: session link and live session ----------------------------------

class SessionPatch(BaseModel):
    lien_session: LineStr | None = None
    liens: list[LineStr] | None = None
    session_ouverte: bool | None = None
    type_diffusion: str | None = Field(default=None, pattern="^(embed|externe|aucun)$")
    visibilite: str | None = Field(default=None, pattern="^(public|membres|prive)$")


@router.patch("/admin/evenements/{evenement_id}/session")
def maj_session(evenement_id: str, payload: SessionPatch, user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))]) -> dict[str, object]:
    """Set the session links, broadcast kind/visibility, and open or close the live session."""
    import json

    sets: list[str] = []
    params: list[object] = []
    if payload.liens is not None:
        liens = [x.strip() for x in payload.liens if x and x.strip()]
        sets.append("liens = %s::jsonb")
        params.append(json.dumps(liens))
        # Keep the legacy primary link in sync with the first entry.
        sets.append("lien_session = %s")
        params.append(liens[0] if liens else None)
    elif payload.lien_session is not None:
        sets.append("lien_session = %s")
        params.append(payload.lien_session or None)
        sets.append("liens = %s::jsonb")
        params.append(json.dumps([payload.lien_session] if payload.lien_session else []))
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
    # When the live session is opened, notify members that the activity has started
    # AND send the attendance survey once (event-driven: reaches the audience right
    # when the activity is launched, and never again thanks to the per-event marker).
    if payload.session_ouverte:
        _notifier_session_ouverte(evenement_id, str(row["lien_session"] or ""), user.role)
        from .sondage import envoyer_sondage_ouverture

        envoyer_sondage_ouverture(evenement_id, user.role)
    return {"id": str(row["id"]), "session_ouverte": bool(row["session_ouverte"]), "lien_session": row["lien_session"]}


@router.post("/admin/evenements/{evenement_id}/test-diffusion")
def test_diffusion(evenement_id: str, user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))]) -> dict[str, object]:
    """Send a 'live broadcast test' notification so members can verify the stream view.

    The message clearly flags itself as a test. Not deduplicated: each explicit
    admin trigger is a fresh test send.
    """
    from .notifications import notifier

    ev = db.fetch_one("SELECT titre FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    # This test fans out to every active member across all channels (including the
    # paid WhatsApp channel), so cap it to one send per event and per hour to stop
    # a looped trigger from amplifying notifications or running up cost.
    if not ratelimit.throttle_action(f"test-diffusion:{evenement_id}", 1, 3600):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="test de diffusion deja envoye pour cet evenement dans la derniere heure",
        )
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
        titre = ev["titre"] if ev else "l'activité"
        for m in db.fetch_all("SELECT id FROM membre WHERE statut = 'actif'", (), role=role):
            notifier(str(m["id"]), role, "activite_demarree", {"titre": titre, "lien": lien}, ref_id=evenement_id, dedup=True)
    except Exception:  # noqa: BLE001 - notifications must never block the session action
        pass


# --- Admin: questionnaire builder -------------------------------------------

class QuestionIn(BaseModel):
    libelle: TitleStr
    type: ShortStr = "texte"  # 'texte' | 'choix' | 'note'
    options: list[LineStr] = []


class QuestionnaireIn(BaseModel):
    titre: TitleStr = "Questionnaire de session"
    questions: list[QuestionIn] = Field(max_length=100)


@router.put("/admin/evenements/{evenement_id}/questionnaire")
def definir_questionnaire(evenement_id: str, payload: QuestionnaireIn, user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))]) -> dict[str, object]:
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
def get_questionnaire_admin(evenement_id: str, user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> dict[str, object] | None:
    q = db.fetch_one("SELECT id, titre FROM questionnaire WHERE evenement_id = %s", (evenement_id,), role=user.role)
    if not q:
        return None
    return {"id": str(q["id"]), "titre": q["titre"], "questions": _questions(str(q["id"]), user.role)}


# Diffusion threshold: below this number of answers, no individual content (rating
# distribution or comment) is revealed, so a small cohort cannot be re-identified.
_SEUIL_DIFFUSION = 3


@router.get("/admin/evenements/{evenement_id}/reponses")
def get_reponses(evenement_id: str, user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> dict[str, object]:
    """Anonymous aggregate of an activity's questionnaire.

    The answers carry NO member link, so nothing here can attribute a rating or a
    comment to a person. Ratings are returned as a mean and a 1..5 distribution;
    free answers as an unordered, author-less list. Below the diffusion threshold
    the content stays hidden to protect small cohorts."""
    q = db.fetch_one("SELECT id, titre FROM questionnaire WHERE evenement_id = %s", (evenement_id,), role=user.role)
    if not q:
        return {"total": 0, "seuil": _SEUIL_DIFFUSION, "seuil_atteint": False, "anonyme": True, "questions": []}
    qid = str(q["id"])
    questions = _questions(qid, user.role)
    rows = db.fetch_all("SELECT reponses FROM reponse_questionnaire WHERE questionnaire_id = %s", (qid,), role=user.role)
    total = len(rows)
    base = {"total": total, "seuil": _SEUIL_DIFFUSION, "titre": q["titre"], "anonyme": True}
    if total < _SEUIL_DIFFUSION:
        # Reveal only the count and the question list, never the content, so 1 or 2
        # answers can never be pinned to the 1 or 2 people who could have sent them.
        return {**base, "seuil_atteint": False,
                "questions": [{"id": x["id"], "libelle": x["libelle"], "type": x["type"]} for x in questions]}
    agg: list[dict[str, object]] = []
    for x in questions:
        vals = [str((r["reponses"] or {}).get(x["id"], "")).strip() for r in rows]
        vals = [v for v in vals if v]
        if x["type"] == "note":
            nums = [int(v) for v in vals if v.isdigit() and 1 <= int(v) <= 5]
            moyenne = round(sum(nums) / len(nums), 2) if nums else None
            distribution = {str(k): nums.count(k) for k in range(1, 6)}
            agg.append({"id": x["id"], "libelle": x["libelle"], "type": "note",
                        "reponses": len(nums), "moyenne": moyenne, "distribution": distribution})
        else:
            # Free text / choice: an author-less list. No order that could correlate
            # with an arrival sequence carries meaning (no timestamp, no member link).
            agg.append({"id": x["id"], "libelle": x["libelle"], "type": x["type"],
                        "reponses": len(vals), "valeurs": vals})
    return {**base, "seuil_atteint": True, "questions": agg}


# --- Member: answer the questionnaire ---------------------------------------

@router.get("/membres/me/evenements/{evenement_id}/questionnaire")
def get_questionnaire_membre(evenement_id: str, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """The questionnaire for an event, with its availability window and whether
    the member has already answered."""
    membre_id, role = ctx
    if not visibilite.evenement_visible_membre(evenement_id, membre_id, role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    ev = db.fetch_one("SELECT fin, session_ouverte FROM evenement WHERE id = %s", (evenement_id,), role=role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    q = db.fetch_one("SELECT id, titre FROM questionnaire WHERE evenement_id = %s AND actif", (evenement_id,), role=role)
    if not q:
        return {"disponible": False, "raison": "aucun_questionnaire"}
    fenetre = _fenetre_heures(role)
    # Available from the moment the session starts, and up to the configured
    # window (default 6 h) after the end (or after the start when no end is set).
    window = db.fetch_one(
        "SELECT (now() >= debut AND now() <= COALESCE(fin, debut) + (%s || ' hours')::interval) AS ouvert, fin FROM evenement WHERE id = %s",
        (str(fenetre), evenement_id),
        role=role,
    )
    # "Already answered" is read from the member-side consumption record, never from
    # the answer content (which no longer carries a member link).
    already = db.fetch_one("SELECT 1 FROM questionnaire_droit WHERE questionnaire_id = %s AND membre_id = %s", (str(q["id"]), membre_id), role=role)
    disponible = bool(window and window["ouvert"]) and not already
    return {
        "disponible": disponible,
        "deja_repondu": bool(already),
        "fenetre_heures": fenetre,
        "titre": q["titre"],
        "questions": _questions(str(q["id"]), role) if disponible else [],
    }


class ReponseIn(BaseModel):
    # Answer values are free text: bound both the number of entries and each
    # value's length so a single submission cannot carry an unbounded payload.
    reponses: dict[ShortStr, LongTextStr] = Field(max_length=200)


@router.post("/membres/me/evenements/{evenement_id}/questionnaire", status_code=status.HTTP_201_CREATED)
def repondre_questionnaire(evenement_id: str, payload: ReponseIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    """Submit answers, only while the questionnaire window is open and once."""
    membre_id, role = ctx
    if not visibilite.evenement_visible_membre(evenement_id, membre_id, role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    q = db.fetch_one("SELECT id FROM questionnaire WHERE evenement_id = %s AND actif", (evenement_id,), role=role)
    if not q:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no questionnaire")
    fenetre = _fenetre_heures(role)
    # Same window as the display (get_questionnaire_membre): open from the session
    # start up to the configured hours after the end (or after the start when no
    # end is set), so what the member is shown as available can always be
    # submitted, and an event without an end is not permanently closed.
    window = db.fetch_one(
        "SELECT (now() >= debut AND now() <= COALESCE(fin, debut) + (%s || ' hours')::interval) AS ouvert FROM evenement WHERE id = %s",
        (str(fenetre), evenement_id),
        role=role,
    )
    if not window or not window["ouvert"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="questionnaire window closed")
    # ANONYMITY BY DESIGN. In a single atomic statement: claim the member's one-time
    # right to answer (questionnaire_droit, member-keyed, NO content) and, only if the
    # claim succeeds, store the answers WITHOUT any member link (reponse_questionnaire
    # carries no member_id and only a coarse date). The member_id therefore never sits
    # next to a note or a comment, and the two rows share no persisted key: the note and
    # comment can never be attributed to the member, while duplicates are still blocked.
    row = db.execute(
        """
        WITH claim AS (
            INSERT INTO questionnaire_droit (questionnaire_id, membre_id) VALUES (%s, %s)
            ON CONFLICT (questionnaire_id, membre_id) DO NOTHING
            RETURNING questionnaire_id
        )
        INSERT INTO reponse_questionnaire (questionnaire_id, reponses)
        SELECT questionnaire_id, %s::jsonb FROM claim
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
    cal_tribu: bool = False  # own-tribe birthdays behind a filter
    cal_coordination: bool = False  # own-coordination birthdays behind a filter
    cal_intendance: bool = False  # own-intendance birthdays behind a filter
    # Per-group, per-channel matrix: {group: {"email": bool, "telegram": bool, ...}}.
    # None or a partial matrix is MERGED over what is already stored (never wipes it).
    # Bounded (max_length) as defence in depth so a huge payload is rejected early.
    matrice_canaux: dict[str, dict[str, bool]] | None = Field(default=None, max_length=64)


_MATRICE_CANAUX = ("email", "telegram", "whatsapp", "sms")


def _sanitize_matrice(raw: object, groupes: tuple[str, ...]) -> dict[str, dict[str, bool]] | None:
    """Keep only known groups and known channels with boolean values, so a client can
    never store an arbitrary shape in the JSONB column."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, dict[str, bool]] = {}
    for groupe, chans in raw.items():
        if groupe not in groupes or not isinstance(chans, dict):
            continue
        clean = {c: bool(chans[c]) for c in _MATRICE_CANAUX if c in chans}
        if clean:
            out[groupe] = clean
    return out or None


_PREF_COLS = (
    "evenements", "demandes", "rappels", "email", "telegram", "whatsapp", "sms",
    "anniversaire", "anniv_pairs", "cal_vip", "cal_responsables", "cal_commission",
    "cal_tribu", "cal_coordination", "cal_intendance",
)
_PREF_OFF_DEFAULT = (
    "whatsapp", "sms", "cal_responsables", "cal_commission",
    "cal_tribu", "cal_coordination", "cal_intendance",
)


def _matrice_complete(stored: object, legacy: dict[str, object] | None = None) -> dict[str, dict[str, bool]]:
    """The full per-group matrix returned to the client: every group is always present.

    For a group the member has not set in ``stored``, the two channels are seeded from
    their legacy per-category opt-out (an old category turned off shows as both channels
    off), so what is displayed matches what is actually delivered. The stored per-group
    choices then override that seed. New groups always appear (default both on)."""
    from .notifications import _GROUPE_LEGACY_COL, _matrice_defaut

    base = _matrice_defaut()
    if legacy:
        for groupe, defaut in base.items():
            col = _GROUPE_LEGACY_COL.get(groupe)
            if col and col in legacy and not legacy[col]:
                base[groupe] = dict.fromkeys(defaut, False)
    if isinstance(stored, dict):
        for groupe, chans in stored.items():
            if groupe in base and isinstance(chans, dict):
                base[groupe] = {**base[groupe], **{c: bool(v) for c, v in chans.items() if c in _MATRICE_CANAUX}}
    return base


def _merge_matrice(stored: object, incoming: dict[str, dict[str, bool]] | None) -> dict[str, dict[str, bool]] | None:
    """Merge the incoming (already sanitised) matrix over what is stored, per group and
    per channel, so an absent or partial matrix NEVER wipes a member's saved choices."""
    out: dict[str, dict[str, bool]] = {}
    if isinstance(stored, dict):
        for groupe, chans in stored.items():
            if isinstance(chans, dict):
                out[groupe] = {c: bool(v) for c, v in chans.items() if c in _MATRICE_CANAUX}
    if isinstance(incoming, dict):
        for groupe, chans in incoming.items():
            out[groupe] = {**out.get(groupe, {}), **chans}
    return out or None


@router.get("/membres/me/preferences-notification")
def get_preferences(ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    membre_id, role = ctx
    row = db.fetch_one(
        f"SELECT {', '.join(_PREF_COLS)}, matrice_canaux FROM preference_notification WHERE membre_id = %s",
        (membre_id,), role=role,
    )
    lie = db.fetch_one("SELECT (telegram_chat_id IS NOT NULL) AS lie FROM membre WHERE id = %s", (membre_id,), role=role)
    telegram_lie = bool(lie and lie.get("lie"))
    if not row:
        defaults: dict[str, object] = {c: True for c in _PREF_COLS}
        defaults.update({c: False for c in _PREF_OFF_DEFAULT})
        defaults["matrice_canaux"] = _matrice_complete(None)
        defaults["telegram_lie"] = telegram_lie
        return defaults
    result: dict[str, object] = {c: bool(row[c]) for c in _PREF_COLS}
    result["matrice_canaux"] = _matrice_complete(row.get("matrice_canaux"), row)
    result["telegram_lie"] = telegram_lie
    return result


@router.put("/membres/me/preferences-notification")
def set_preferences(payload: PreferencesIn, ctx: Annotated[tuple[str, str], Depends(_membre)]) -> dict[str, object]:
    from .notifications import GROUPES

    membre_id, role = ctx
    values = {c: getattr(payload, c) for c in _PREF_COLS}
    incoming = _sanitize_matrice(payload.matrice_canaux, GROUPES)
    # Merge over the stored matrix so a partial or absent matrix never wipes a saved
    # channel choice (a client that omits matrice_canaux keeps the member's existing one).
    existing = db.fetch_one("SELECT matrice_canaux FROM preference_notification WHERE membre_id = %s", (membre_id,), role=role)
    matrice = _merge_matrice(existing.get("matrice_canaux") if existing else None, incoming)
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in _PREF_COLS)
    db.execute(
        f"""
        INSERT INTO preference_notification (membre_id, {', '.join(_PREF_COLS)}, matrice_canaux, maj_le)
        VALUES (%s, {', '.join(['%s'] * len(_PREF_COLS))}, %s::jsonb, now())
        ON CONFLICT (membre_id) DO UPDATE SET {assignments}, matrice_canaux = EXCLUDED.matrice_canaux, maj_le = now()
        """,
        (membre_id, *[values[c] for c in _PREF_COLS], json.dumps(matrice) if matrice is not None else None),
        role=role,
    )
    result: dict[str, object] = dict(values)
    result["matrice_canaux"] = _matrice_complete(matrice, values)
    lie = db.fetch_one("SELECT (telegram_chat_id IS NOT NULL) AS lie FROM membre WHERE id = %s", (membre_id,), role=role)
    result["telegram_lie"] = bool(lie and lie.get("lie"))
    return result


# --- Admin: questionnaire window duration -----------------------------------

class FenetreIn(BaseModel):
    heures: int


@router.get("/admin/parametres/questionnaire-fenetre")
def get_fenetre(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, int]:
    return {"heures": _fenetre_heures(user.role)}


@router.put("/admin/parametres/questionnaire-fenetre")
def set_fenetre(payload: FenetreIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, int]:
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


# --- Admin: first day of the week (weekly recap/agenda bounds) ---------------

class SemaineDebutIn(BaseModel):
    jour: int  # 0 = Monday ... 6 = Sunday


@router.get("/admin/parametres/semaine-jour-debut")
def get_semaine_debut(user: Annotated[UserMe, Depends(require_permission("parametres.consulter"))]) -> dict[str, int]:
    row = db.fetch_one("SELECT (valeur #>> '{}')::int AS j FROM parametre WHERE cle = 'semaine_jour_debut'", (), role=user.role)
    return {"jour": int(row["j"]) if row and row.get("j") is not None else 0}


@router.put("/admin/parametres/semaine-jour-debut")
def set_semaine_debut(payload: SemaineDebutIn, user: Annotated[UserMe, Depends(require_permission("parametres.gerer"))]) -> dict[str, int]:
    if not 0 <= payload.jour <= 6:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="jour must be between 0 (Monday) and 6 (Sunday)")
    db.execute(
        """
        INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le)
        VALUES ('semaine_jour_debut', %s::jsonb, 'notifications', 'First day of the week for weekly recap/agenda (0=Monday)', %s, now())
        ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()
        """,
        (str(payload.jour), user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "config_semaine_jour_debut", "parametre", "semaine_jour_debut", {"jour": payload.jour})
    return {"jour": payload.jour}
