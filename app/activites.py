"""Shared activity engine.

A real activity is a row in the ``evenement`` table: it feeds the member agenda,
attendance and questionnaires through the existing targeting rules. Several callers
create activities: the back office (full form in ``admin.py``), the pilotage layer
(a responsable, bounded to their perimeter) and the collaboration spaces (a card
published as an activity). They must all land the exact same row shape and honour
the same targeting validation, so that single engine lives here.

The engine deliberately never forces ``session_ouverte``: an activity is opened for
attendance separately, when its session actually starts. Broadcasting to everyone
(``general``) stays a caller-side decision; this module only validates that a
targeted activity names an existing unit.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from . import audit, db
from .sanitize import sanitize_html

# Organisational units a targeted activity can address, mapped to their table.
CIBLE_UNITES = {
    "coordination": "coordination",
    "commission": "commission",
    "intendance": "intendance",
    "tribu": "tribu",
}

# Every target kind the simple engine accepts. ``general`` reaches all members;
# the unit kinds reach one organisational unit; refinements (gender, age, list)
# stay reserved to the full back-office form.
TYPES_CIBLE = ("general", *CIBLE_UNITES.keys())


def valider_cible(cible_type: str, cible_id: str | None, role: str | None) -> str | None:
    """Validate a simple activity target and return the unit id to store.

    Parameters
    ----------
    cible_type : str
        One of :data:`TYPES_CIBLE`.
    cible_id : str or None
        The organisational unit id, required for a unit target, ignored otherwise.
    role : str or None
        The caller role, used for the row-level security context of the lookup.

    Returns
    -------
    str or None
        The validated unit id, or ``None`` for a ``general`` target.

    Raises
    ------
    HTTPException
        400 if the kind is unknown, the id is missing for a unit target, or the
        named unit does not exist.
    """
    if cible_type not in TYPES_CIBLE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_type invalide")
    if cible_type == "general":
        return None
    if not cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id requis pour une activite ciblee")
    table = CIBLE_UNITES[cible_type]
    if not db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (cible_id,), role=role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unite ciblee introuvable")
    return cible_id


def inserer_evenement(
    *,
    titre: str,
    type_: str | None,
    debut: object,
    fin: object = None,
    lieu: str | None = None,
    mode: str | None = None,
    cible_type: str = "general",
    cible_id: str | None = None,
    visibilite: str = "membres",
    cree_par: str,
    role: str | None,
) -> str:
    """Insert one real activity and return its id.

    The target is validated with :func:`valider_cible`; ``session_ouverte`` is left
    to its schema default (closed) so a freshly created activity is never opened for
    attendance before its session starts.
    """
    stored_id = valider_cible(cible_type, cible_id, role)
    created = db.execute(
        "INSERT INTO evenement (titre, type, mode, debut, fin, lieu, cible_type, cible_id, visibilite, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (titre, type_, mode, debut, fin, lieu, cible_type, stored_id, visibilite, cree_par),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation activite impossible")
    return str(created["id"])


# --- Full event form (shared by the back office and collaboration) --------------
# The complete create/update logic behind the rich activity form. It is the single
# source of truth so the back office and the collaboration app offer exactly the
# same fields, validation, targeting, recurrence and automations; each caller only
# differs by the permission that guards it.

def valider_cible_complet(cible_type: str, cible_id: str | None, role: str | None) -> str | None:
    """Validate the PRIMARY audience of the full form. A unit target must name an
    existing unit; general / bergers / responsables / liste carry no unit id."""
    if cible_type not in CIBLE_UNITES:
        return None
    if not cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id required for a targeted event")
    if not db.fetch_one(f"SELECT id FROM {CIBLE_UNITES[cible_type]} WHERE id = %s", (cible_id,), role=role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown target unit")
    return cible_id


def cible_store(payload: Any, role: str | None) -> tuple[str | None, str | None, int | None, int | None, list[str]]:
    """Validate and normalise the full targeting to store: the unit id (or None),
    the gender/age refinements and the de-duplicated e-mail list (only for 'liste')."""
    cible_id = valider_cible_complet(payload.cible_type, payload.cible_id, role)
    emails: list[str] = []
    if payload.cible_type == "liste":
        emails = list(dict.fromkeys(e.strip().lower() for e in payload.cible_emails if e and e.strip()))[:500]
        if not emails:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="au moins une adresse e-mail est requise pour un ciblage par liste",
            )
    return cible_id, payload.cible_genre, payload.cible_age_min, payload.cible_age_max, emails


def serie_ids(evenement_id: str, portee: str, role: str | None) -> list[str]:
    """Every activity id of the series when portee='toute_la_serie', else the one."""
    if portee != "toute_la_serie":
        return [evenement_id]
    row = db.fetch_one("SELECT serie_id FROM evenement WHERE id = %s", (evenement_id,), role=role)
    serie = row.get("serie_id") if row else None
    if not serie:
        return [evenement_id]
    rows = db.fetch_all("SELECT id FROM evenement WHERE serie_id = %s", (str(serie),), role=role)
    return [str(r["id"]) for r in rows] or [evenement_id]


def _liens_normalises(payload: Any) -> tuple[str | None, str]:
    """The primary session link and the JSON-encoded de-duplicated link list."""
    liens = [x.strip() for x in payload.liens if x and x.strip()]
    primary = payload.lien_session or (liens[0] if liens else None)
    if primary and primary not in liens:
        liens = [primary, *liens]
    return primary, json.dumps(liens)


def creer_evenement_complet(payload: Any, cree_par: str, role: str | None) -> str:
    """Create an activity from the full form and return its id. Handles targeting,
    diffusion links, response window, time zone and recurrence (a non-empty
    ``occurrences`` makes it a series sharing a serie_id). Mirrors the back office."""
    primary, liens_json = _liens_normalises(payload)
    cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails = cible_store(payload, role)
    description = sanitize_html(payload.description)
    intervenants = [x.strip() for x in payload.intervenants if x and x.strip()][:30]
    principal = (payload.intervenant_principal or "").strip() or None
    created = db.execute(
        "INSERT INTO evenement (titre, type, volet, debut, fin, lieu, mode, lien_session, liens, type_diffusion, "
        "visibilite, cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails, "
        "fenetre_reponse_heures, fuseau_horaire, description, intervenant_principal, intervenants, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s, %s, %s, "
        "%s::text[], %s) RETURNING id",
        (
            payload.titre, payload.type, payload.volet, payload.debut, payload.fin, payload.lieu,
            payload.mode, primary, liens_json, payload.type_diffusion, payload.visibilite,
            payload.cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails,
            payload.fenetre_reponse_heures, payload.fuseau_horaire, description, principal, intervenants, cree_par,
        ),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    master_id = str(created["id"])
    audit.log(cree_par, role, "creation_evenement", "evenement", master_id,
              {"titre": payload.titre, "cible_type": payload.cible_type})
    if payload.occurrences:
        db.execute(
            "UPDATE evenement SET serie_id = %s, recurrence = %s::jsonb WHERE id = %s",
            (master_id, json.dumps(payload.recurrence) if payload.recurrence else None, master_id),
            role=role,
        )
        for occ in payload.occurrences:
            db.execute(
                "INSERT INTO evenement (titre, type, volet, debut, fin, lieu, mode, lien_session, liens, "
                "type_diffusion, visibilite, cible_type, cible_id, fenetre_reponse_heures, fuseau_horaire, "
                "serie_id, cree_par) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    payload.titre, payload.type, payload.volet, occ.debut, occ.fin, payload.lieu,
                    occ.mode or payload.mode, primary, liens_json, payload.type_diffusion, payload.visibilite,
                    payload.cible_type, cible_id, payload.fenetre_reponse_heures, payload.fuseau_horaire,
                    master_id, cree_par,
                ),
                role=role,
            )
        audit.log(cree_par, role, "creation_serie_evenement", "evenement", master_id,
                  {"titre": payload.titre, "occurrences": len(payload.occurrences) + 1})
    return master_id


def mettre_a_jour_evenement_complet(
    evenement_id: str, payload: Any, portee: str, actor_id: str, role: str | None
) -> None:
    """Edit an activity from the full form (same fields as create). With
    portee='toute_la_serie' the non-date details apply to every activity of the
    series while each occurrence keeps its own date. Mirrors the back office."""
    if not db.fetch_one("SELECT id FROM evenement WHERE id = %s", (evenement_id,), role=role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    primary, liens_json = _liens_normalises(payload)
    cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails = cible_store(payload, role)
    description = sanitize_html(payload.description)
    intervenants = [x.strip() for x in payload.intervenants if x and x.strip()][:30]
    principal = (payload.intervenant_principal or "").strip() or None
    db.execute(
        "UPDATE evenement SET titre=%s, type=%s, volet=%s, debut=%s, fin=%s, lieu=%s, mode=%s, "
        "lien_session=%s, liens=%s::jsonb, type_diffusion=%s, visibilite=%s, cible_type=%s, "
        "cible_id=%s, cible_genre=%s, cible_age_min=%s, cible_age_max=%s, cible_emails=%s::text[], "
        "fenetre_reponse_heures=%s, fuseau_horaire=%s, description=%s, intervenant_principal=%s, "
        "intervenants=%s::text[] WHERE id=%s",
        (
            payload.titre, payload.type, payload.volet, payload.debut, payload.fin, payload.lieu,
            payload.mode, primary, liens_json, payload.type_diffusion, payload.visibilite,
            payload.cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails,
            payload.fenetre_reponse_heures, payload.fuseau_horaire, description, principal, intervenants, evenement_id,
        ),
        role=role,
    )
    if portee == "toute_la_serie":
        for other in (i for i in serie_ids(evenement_id, portee, role) if i != evenement_id):
            db.execute(
                "UPDATE evenement SET titre=%s, type=%s, volet=%s, lieu=%s, mode=%s, "
                "lien_session=%s, liens=%s::jsonb, type_diffusion=%s, visibilite=%s, "
                "cible_type=%s, cible_id=%s, cible_genre=%s, cible_age_min=%s, cible_age_max=%s, "
                "cible_emails=%s::text[], fenetre_reponse_heures=%s, fuseau_horaire=%s, description=%s, "
                "intervenant_principal=%s, intervenants=%s::text[] WHERE id=%s",
                (
                    payload.titre, payload.type, payload.volet, payload.lieu, payload.mode,
                    primary, liens_json, payload.type_diffusion, payload.visibilite,
                    payload.cible_type, cible_id, cible_genre, cible_age_min, cible_age_max, cible_emails,
                    payload.fenetre_reponse_heures, payload.fuseau_horaire, description, principal, intervenants, other,
                ),
                role=role,
            )
    audit.log(actor_id, role, "modification_evenement", "evenement", evenement_id,
              {"titre": payload.titre, "portee": portee})
