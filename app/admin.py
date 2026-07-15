"""Back-office administration endpoints, served from the real PostgreSQL.

Every query sets the caller role for the per-role RLS policies (ADR-0002), which
act as defense in depth: the backend connects as the table owner, which bypasses
RLS, so access is enforced first by ``require_roles`` (which rejects unauthorized
callers before touching the database) and by the explicit ``WHERE`` scoping of
each query, not by RLS alone.
"""
# ruff: noqa: E501
from __future__ import annotations

import uuid
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from . import activites, audit, db, evenement_pieces, identifiants, identite
from .mappers import MEMBRE_PROFILE_FROM, MEMBRE_PROFILE_SELECT, membre_row_to_profile
from .membre_filters import exclusion_technique
from .permissions_rbac import require_permission
from .schemas import (
    CommissionOut,
    CreateCommission,
    CreateEvenement,
    CreateMembre,
    EvenementOut,
    MembreProfile,
    UpdateMembre,
    UserMe,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# Reading a member's full personal record (address, phone, DOB, marital status)
# is not needed by a field scanner (controleur) nor read-only supervision
# (direction): keep the nominative directory to the member-managing roles.


# Governance and state fields whose before/after values are safe to record in
# the audit trail: they carry accountability (validation, membership state,
# pastoral status) without free personal data. The confidential note and raw
# PII fields are deliberately excluded, so the audit stays minimal (RGPD).
_AUDITED_VALUE_FIELDS = (
    "statut",
    "verifie",
    "appartenance",
    "est_berger",
    "type_membre",
    "fonction_cle",
    "fonction_perimetre",
    "nom_affiche",
)


def _read_membre(membre_id: str, role: str) -> MembreProfile:
    row = db.fetch_one(
        f"SELECT {MEMBRE_PROFILE_SELECT} {MEMBRE_PROFILE_FROM} WHERE m.id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    from . import fonctions_membre

    fonctions = fonctions_membre.fonctions_publiques(membre_id, row.get("genre"), role)
    return membre_row_to_profile(row, fonctions)


@router.post("/membres", response_model=MembreProfile, status_code=status.HTTP_201_CREATED)
def create_membre(
    payload: CreateMembre,
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
) -> MembreProfile:
    matricule = payload.matricule or identifiants.next_matricule(user.role, payload.nom, payload.prenoms)
    data = payload.model_dump(exclude_unset=True, exclude={"matricule"})
    data["matricule"] = matricule
    data.setdefault("statut", "actif")
    data.setdefault("verifie", False)
    # Normalise the civil identity (family name uppercase, given names title case).
    for champ, fonction in (("nom", identite.normaliser_nom), ("prenoms", identite.normaliser_prenoms),
                            ("nom_naissance", identite.normaliser_nom), ("nom_marital", identite.normaliser_nom),
                            ("nom_pastoral", identite.normaliser_prenoms)):
        if data.get(champ) is not None:
            data[champ] = fonction(str(data[champ]))
    columns = ", ".join(data)
    placeholders = ", ".join(["%s"] * len(data))
    try:
        created = db.execute(
            f"INSERT INTO membre ({columns}) VALUES ({placeholders}) RETURNING id",
            tuple(data.values()),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email or matricule already in use",
        ) from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    membre_id = str(created["id"])
    audit.log(user.id, user.role, "creation_membre", "membre", membre_id, {"matricule": matricule})
    return _read_membre(membre_id, user.role)


# Directory compartments: each maps to an explicit predicate on the validation status
# (statut_inscription) and the operational status (statut). A non-validated dossier
# (submitted, in review, correction requested, refused, incomplete) NEVER appears in the
# "actifs" compartment. Keys are the only accepted values (no raw SQL from the client).
_COMPARTIMENTS: dict[str, str] = {
    "actifs": "m.statut = 'actif' AND m.statut_inscription = 'approuve' AND m.verifie = true",
    "en_attente": "m.statut_inscription IN ('soumis', 'en_revue')",
    "corrections": "m.statut_inscription = 'modification_demandee'",
    "refuses": "m.statut_inscription = 'refuse'",
    "incomplets": "m.statut_inscription = 'incomplet'",
    "inactifs": "m.statut = 'inactif'",
    "suspendus": "m.statut = 'suspendu'",
    "archives": "m.statut = 'archive'",
    "tous": "",
}
# A technical/applicative user is NEVER a member and must never appear in the member
# directory. Technical accounts have no member row, so they are naturally absent; this
# predicate is a defensive guarantee that no member row tied to a technical identity ever
# leaks into the directory, its compartments or its counts. A row is excluded when it is
# linked to a technical account by membre_id OR merely shares its e-mail (a stray member
# row carrying a technical account's address, such as the founder's, must never surface).
_PAS_TECHNIQUE = exclusion_technique("m")
# Whitelisted sort orders. Default is alphabetical by family name then first names,
# case-insensitive, with the matricule as a deterministic final tiebreaker.
_TRIS: dict[str, str] = {
    "nom": "lower(m.nom) ASC NULLS LAST, lower(m.prenoms) ASC NULLS LAST, m.matricule ASC",
    "nom_desc": "lower(m.nom) DESC NULLS LAST, lower(m.prenoms) DESC NULLS LAST, m.matricule ASC",
    "inscription": "m.cree_le DESC NULLS LAST, m.matricule ASC",
    "matricule": "m.matricule ASC",
}


@router.get("/membres/compartiments")
def compartiments_membres(
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
) -> dict[str, int]:
    """Exact count of members in each directory compartment, so the tabs show a live,
    correct badge and a non-validated dossier is never mixed with the active members."""
    out: dict[str, int] = {}
    for key, pred in _COMPARTIMENTS.items():
        where = f"WHERE {_PAS_TECHNIQUE}" + (f" AND {pred}" if pred else "")
        row = db.fetch_one(f"SELECT count(*) AS n FROM membre m {where}", (), role=user.role)
        out[key] = int((row or {}).get("n") or 0)
    return out


@router.get("/membres", response_model=list[MembreProfile])
def list_membres(
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
    q: str | None = Query(default=None),
    pays: str | None = Query(default=None),
    commission_id: str | None = Query(default=None),
    intendance_id: str | None = Query(default=None),
    coordination_id: str | None = Query(default=None),
    tribu_id: str | None = Query(default=None),
    statut: str | None = Query(default=None),
    tri: str = Query(default="nom"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembreProfile]:
    params: list[object] = []
    # A technical/applicative user never appears in the member directory.
    conditions: list[str] = [_PAS_TECHNIQUE]
    # Compartment predicate (validation/operational status). Unknown key -> no filter,
    # so an old client without the param still gets the full directory.
    compartiment_pred = _COMPARTIMENTS.get(statut or "", "") if statut else ""
    if compartiment_pred:
        conditions.append(compartiment_pred)
    if q:
        # Escape LIKE metacharacters so a literal % or _ typed in the search is not
        # treated as a wildcard (default backslash escape). The OR-group is
        # parenthesised so it composes correctly with the AND filters below.
        q_esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_esc}%"
        conditions.append(
            "(m.nom ILIKE %s OR m.prenoms ILIKE %s OR m.matricule ILIKE %s "
            "OR m.email ILIKE %s OR m.code_membre ILIKE %s)"
        )
        params.extend([like, like, like, like, like])
    # Exact-match facets used by the directory side panel. Each is optional and
    # combined with AND, so filters narrow the result set cumulatively.
    for column, value in (
        ("m.pays", pays),
        ("m.commission_id", commission_id),
        ("m.intendance_id", intendance_id),
        ("m.coordination_id", coordination_id),
        ("m.tribu_id", tribu_id),
    ):
        if value:
            conditions.append(f"{column} = %s")
            params.append(value)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order_by = _TRIS.get(tri, _TRIS["nom"])
    params.extend([limit, offset])
    rows = db.fetch_all(
        f"""
        SELECT {MEMBRE_PROFILE_SELECT}
        {MEMBRE_PROFILE_FROM}
        {where}
        ORDER BY {order_by}
        LIMIT %s OFFSET %s
        """,
        tuple(params),
        role=user.role,
    )
    return [membre_row_to_profile(r) for r in rows]


@router.get("/annuaire/pays")
def annuaire_pays(
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
) -> list[str]:
    """Distinct, non-empty countries present among members, to populate the
    directory side-panel country filter with values that actually match data."""
    rows = db.fetch_all(
        "SELECT DISTINCT pays FROM membre WHERE pays IS NOT NULL AND btrim(pays) <> '' ORDER BY pays",
        (),
        role=user.role,
    )
    return [str(r["pays"]) for r in rows]


@router.get("/membres/{membre_id}", response_model=MembreProfile)
def get_membre(
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
) -> MembreProfile:
    return _read_membre(membre_id, user.role)


def _verifier_axe_orga_exclusif(membre_id: str, fields: dict[str, object], role: str) -> None:
    """Reject setting both a coordination and an intendance on a member.

    A member belongs to a coordination OR an intendance (business rule R3, also a
    DB CHECK). The effective value of each axis is the one in this update when
    present, else the value already stored. Raising here gives a clean 400 rather
    than surfacing the constraint violation as a 500.
    """
    if "coordination_id" not in fields and "intendance_id" not in fields:
        return
    current = db.fetch_one(
        "SELECT coordination_id, intendance_id FROM membre WHERE id = %s", (membre_id,), role=role
    ) or {}
    coord = fields.get("coordination_id") if "coordination_id" in fields else current.get("coordination_id")
    intend = fields.get("intendance_id") if "intendance_id" in fields else current.get("intendance_id")
    if coord and intend:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="un membre ne peut pas appartenir a la fois a une coordination et a une intendance",
        )


@router.patch("/membres/{membre_id}", response_model=MembreProfile)
def update_membre(
    membre_id: str,
    payload: UpdateMembre,
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
) -> MembreProfile:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return _read_membre(membre_id, user.role)
    _verifier_axe_orga_exclusif(membre_id, fields, user.role)
    for champ, fonction in (("nom", identite.normaliser_nom), ("prenoms", identite.normaliser_prenoms),
                            ("nom_naissance", identite.normaliser_nom), ("nom_marital", identite.normaliser_nom),
                            ("nom_pastoral", identite.normaliser_prenoms)):
        if fields.get(champ) is not None:
            fields[champ] = fonction(str(fields[champ]))
    # The external member code is always stored uppercase; an empty string clears it.
    if "code_membre" in fields:
        fields["code_membre"] = (str(fields["code_membre"]).strip().upper() or None) if fields["code_membre"] is not None else None
    # Snapshot the prior values of the audited governance fields before writing,
    # so the trail records the actual state change (e.g. verifie false -> true).
    audited = [name for name in fields if name in _AUDITED_VALUE_FIELDS]
    before = (
        db.fetch_one(
            f"SELECT {', '.join(audited)} FROM membre WHERE id = %s",
            (membre_id,),
            role=user.role,
        )
        if audited
        else None
    )
    columns = ", ".join(f"{name} = %s" for name in fields)
    params = [*fields.values(), membre_id]
    try:
        updated = db.execute(
            f"UPDATE membre SET {columns} WHERE id = %s RETURNING id",
            tuple(params),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email or matricule already in use",
        ) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # Validating a member's identity from the member sheet must also settle the
    # registration lifecycle: otherwise the member stays VERIFIE + ACTIF in the
    # back office yet blocked on the "inscription en cours d'examen" screen because
    # statut_inscription was left at 'soumis'/'en_revue'. Advancing it to 'approuve'
    # keeps the two validation paths consistent (only pending states are touched:
    # a dossier still 'incomplet'/'modification_demandee' or 'refuse' is untouched),
    # and reproduces the approval side effects of the official review decision
    # (country attestation, data-retention window) so nothing is silently skipped.
    if fields.get("verifie") is True:
        promu = db.execute(
            "UPDATE membre SET statut_inscription = 'approuve', motif_refus = NULL, "
            "champs_a_corriger = NULL, decision_le = now(), decision_par = %s "
            "WHERE id = %s AND statut_inscription IN ('soumis', 'en_revue') RETURNING id",
            (user.id, membre_id),
            role=user.role,
        )
        if promu:
            from .matrice_pays import ouvrir_attestation_si_besoin
            from .retention import set_retention

            ouvrir_attestation_si_besoin(membre_id, user.role)
            set_retention(membre_id, user.role)
            audit.log(user.id, user.role, "decision_inscription", "membre", membre_id,
                      {"decision": "approuve", "via": "validation_identite"})
    action = "validation_identite" if fields.get("verifie") is True else "modification_membre"
    # Trace field names always; record before/after only for the whitelisted
    # governance fields, never the confidential note nor raw personal data.
    details: dict[str, object] = {"champs": list(fields)}
    if audited:
        details["avant"] = {name: (before.get(name) if before else None) for name in audited}
        details["apres"] = {name: fields[name] for name in audited}
    audit.log(user.id, user.role, action, "membre", membre_id, details)
    return _read_membre(membre_id, user.role)


@router.get("/membres/{membre_id}/gouvernance")
def membre_gouvernance(
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("membres.administrer"))],
) -> dict[str, object]:
    """Admin-only governance block for a member: membership relationship state,
    an optional confidential internal note (reason for a departure, a title
    withdrawal...), and the consecration-title grant date. This is NEVER part of
    the member-facing profile; only a member writer can read it here."""
    row = db.fetch_one(
        "SELECT appartenance, note_confidentielle, berger_depuis FROM membre WHERE id = %s",
        (membre_id,),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return {
        "appartenance": row.get("appartenance") or "actif",
        "note_confidentielle": row.get("note_confidentielle"),
        "berger_depuis": row["berger_depuis"].isoformat() if row.get("berger_depuis") else None,
    }


@router.get("/commissions", response_model=list[CommissionOut])
def list_commissions(user: Annotated[UserMe, Depends(require_permission("commissions.consulter"))]) -> list[CommissionOut]:
    rows = db.fetch_all(
        "SELECT id, nom, description, publie, type_organisation FROM commission ORDER BY type_organisation, nom ASC",
        (),
        role=user.role,
    )
    return [
        CommissionOut(
            id=str(r["id"]), nom=r["nom"], description=r["description"], publie=bool(r["publie"]),
            type_organisation=r.get("type_organisation") or "commission",
        )
        for r in rows
    ]


@router.post("/commissions", response_model=CommissionOut, status_code=status.HTTP_201_CREATED)
def create_commission(
    payload: CreateCommission,
    user: Annotated[UserMe, Depends(require_permission("commissions.administrer"))],
) -> CommissionOut:
    created = db.execute(
        "INSERT INTO commission (nom, description, type_organisation) VALUES (%s, %s, %s) "
        "RETURNING id, nom, description, type_organisation",
        (payload.nom, payload.description, payload.type_organisation),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    audit.log(user.id, user.role, "creation_commission", "commission", str(created["id"]), {"nom": created["nom"]})
    return CommissionOut(
        id=str(created["id"]),
        nom=created["nom"],
        description=created["description"],
        type_organisation=created.get("type_organisation") or "commission",
    )


@router.get("/evenements", response_model=list[EvenementOut])
def list_evenements(user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> list[EvenementOut]:
    rows = db.fetch_all(
        """
        SELECT e.id, e.titre, e.type, e.volet, e.debut, e.fin, e.lieu, e.mode, e.session_ouverte,
               e.lien_session, e.liens, e.type_diffusion, e.visibilite, e.cible_type, e.cible_id,
               e.annule, e.annule_motif, e.fuseau_horaire, e.serie_id, e.fenetre_reponse_heures,
               e.cible_genre, e.cible_age_min, e.cible_age_max, e.cible_emails,
               e.description, e.intervenant_principal, e.intervenants,
               e.type_evenement_id, te.couleur AS couleur, te.nom AS type_evenement_nom,
               CASE e.cible_type
                 WHEN 'coordination' THEN (SELECT nom FROM coordination WHERE id = e.cible_id)
                 WHEN 'commission' THEN (SELECT nom FROM commission WHERE id = e.cible_id)
                 WHEN 'intendance' THEN (SELECT nom FROM intendance WHERE id = e.cible_id)
                 WHEN 'tribu' THEN (SELECT nom FROM tribu WHERE id = e.cible_id)
                 WHEN 'bergers' THEN 'Les Bergers'
                 WHEN 'responsables' THEN 'Les responsables'
                 WHEN 'liste' THEN 'Liste de diffusion'
                 ELSE NULL
               END AS cible_libelle,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.id::text, 'cle', t.cle, 'libelle', t.libelle) ORDER BY t.libelle)
                         FROM evenement_tag et JOIN tag t ON t.id = et.tag_id WHERE et.evenement_id = e.id), '[]'::jsonb) AS tags
        FROM evenement e
        LEFT JOIN type_evenement te ON te.id = e.type_evenement_id
        ORDER BY e.debut DESC
        LIMIT 200
        """,
        (),
        role=user.role,
    )
    return [_evenement_out(r) for r in rows]


def _evenement_out(r: dict[str, object]) -> EvenementOut:
    return EvenementOut(
        id=str(r["id"]),
        titre=r["titre"],
        type=r["type"],
        type_evenement_id=str(r["type_evenement_id"]) if r.get("type_evenement_id") else None,
        couleur=r.get("couleur") if isinstance(r.get("couleur"), str) else None,
        type_evenement_nom=r.get("type_evenement_nom") if isinstance(r.get("type_evenement_nom"), str) else None,
        volet=r["volet"],
        debut=r["debut"],
        fin=r["fin"],
        lieu=r["lieu"],
        mode=r.get("mode"),
        session_ouverte=r["session_ouverte"],
        lien_session=r["lien_session"],
        liens=[str(x) for x in (r.get("liens") or []) if x],
        type_diffusion=r.get("type_diffusion") or "aucun",
        visibilite=r.get("visibilite") or "membres",
        cible_type=r.get("cible_type") or "general",
        cible_id=str(r["cible_id"]) if r.get("cible_id") else None,
        cible_libelle=r.get("cible_libelle"),
        cible_genre=r.get("cible_genre"),
        cible_age_min=r.get("cible_age_min"),
        cible_age_max=r.get("cible_age_max"),
        cible_emails=[str(x) for x in (r.get("cible_emails") or [])],
        tags=[{"id": str(x.get("id")), "cle": str(x.get("cle") or ""), "libelle": str(x.get("libelle") or "")} for x in (r.get("tags") or [])],
        annule=bool(r.get("annule")),
        annule_motif=r.get("annule_motif") if isinstance(r.get("annule_motif"), str) else None,
        fuseau_horaire=r.get("fuseau_horaire") or "Africa/Abidjan",
        serie_id=str(r["serie_id"]) if r.get("serie_id") else None,
        fenetre_reponse_heures=r.get("fenetre_reponse_heures"),
        description=r.get("description") if isinstance(r.get("description"), str) else None,
        intervenant_principal=r.get("intervenant_principal") if isinstance(r.get("intervenant_principal"), str) else None,
        intervenants=[str(x) for x in (r.get("intervenants") or [])],
    )


@router.post("/evenements", response_model=EvenementOut, status_code=status.HTTP_201_CREATED)
def create_evenement(
    payload: CreateEvenement,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> EvenementOut:
    # The full create logic (targeting, diffusion, response window, recurrence) is
    # the shared activity engine, so the back office and collaboration create the
    # exact same activity from the exact same fields.
    master_id = activites.creer_evenement_complet(payload, user.id, user.role)
    return _lire_evenement(master_id, user.role)


class AnnulerEvenementIn(BaseModel):
    motif: str | None = None


def _lire_evenement(evenement_id: str, role: str | None) -> EvenementOut:
    """Re-read one event with its cible_libelle, for an endpoint response."""
    row = db.fetch_one(
        """
        SELECT e.id, e.titre, e.type, e.volet, e.debut, e.fin, e.lieu, e.mode, e.session_ouverte,
               e.lien_session, e.liens, e.type_diffusion, e.visibilite, e.cible_type, e.cible_id,
               e.annule, e.annule_motif, e.fuseau_horaire, e.serie_id, e.fenetre_reponse_heures,
               e.cible_genre, e.cible_age_min, e.cible_age_max, e.cible_emails,
               e.description, e.intervenant_principal, e.intervenants,
               e.type_evenement_id, te.couleur AS couleur, te.nom AS type_evenement_nom,
               CASE e.cible_type
                 WHEN 'coordination' THEN (SELECT nom FROM coordination WHERE id = e.cible_id)
                 WHEN 'commission' THEN (SELECT nom FROM commission WHERE id = e.cible_id)
                 WHEN 'intendance' THEN (SELECT nom FROM intendance WHERE id = e.cible_id)
                 WHEN 'tribu' THEN (SELECT nom FROM tribu WHERE id = e.cible_id)
                 WHEN 'bergers' THEN 'Les Bergers'
                 WHEN 'responsables' THEN 'Les responsables'
                 WHEN 'liste' THEN 'Liste de diffusion'
                 ELSE NULL
               END AS cible_libelle,
               COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.id::text, 'cle', t.cle, 'libelle', t.libelle) ORDER BY t.libelle)
                         FROM evenement_tag et JOIN tag t ON t.id = et.tag_id WHERE et.evenement_id = e.id), '[]'::jsonb) AS tags
        FROM evenement e
        LEFT JOIN type_evenement te ON te.id = e.type_evenement_id
        WHERE e.id = %s
        """,
        (evenement_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return _evenement_out(row)


def _serie_ids(evenement_id: str, portee: str, role: str | None) -> list[str]:
    """Activity ids to act on: just this one, or every activity of its series.
    Shared with the collaboration app through the activity engine."""
    return activites.serie_ids(evenement_id, portee, role)


@router.put("/evenements/{evenement_id}", response_model=EvenementOut)
def update_evenement(
    evenement_id: str,
    payload: CreateEvenement,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
    portee: str = Query(default="cette_occurrence", pattern="^(cette_occurrence|toute_la_serie)$"),
) -> EvenementOut:
    """Edit an activity's details after creation (same fields and validation as create).

    With portee='toute_la_serie' the non-date details (title, type, place, mode,
    links, diffusion, visibility, targeting, zone) are applied to every activity
    of the series, while each occurrence keeps its own date."""
    # Same shared engine as create: identical fields, validation and series handling
    # for the back office and collaboration.
    activites.mettre_a_jour_evenement_complet(evenement_id, payload, portee, user.id, user.role)
    return _lire_evenement(evenement_id, user.role)


@router.post("/evenements/{evenement_id}/annuler", response_model=EvenementOut)
def annuler_evenement(
    evenement_id: str,
    payload: AnnulerEvenementIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
    portee: str = Query(default="cette_occurrence", pattern="^(cette_occurrence|toute_la_serie)$"),
) -> EvenementOut:
    """Cancel an activity (kept for history). It stops triggering the attendance
    survey and the audience is notified once that it is called off. With
    portee='toute_la_serie' every activity of the series is cancelled, and the
    audience is still notified only once."""
    ev = db.fetch_one("SELECT id, titre FROM evenement WHERE id = %s", (evenement_id,), role=user.role)
    if not ev:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    ids = _serie_ids(evenement_id, portee, user.role)
    for eid in ids:
        db.execute(
            "UPDATE evenement SET annule = true, annule_le = now(), annule_motif = %s WHERE id = %s",
            (payload.motif, eid),
            role=user.role,
        )
    from .notifications import notifier
    from .visibilite import CIBLE_PREDICATE

    cibles = db.fetch_all(
        "SELECT m.id, m.prenoms FROM membre m "
        "LEFT JOIN intendance mi ON mi.id = m.intendance_id "
        "JOIN evenement e ON e.id = %s "
        f"WHERE m.statut = 'actif' AND {CIBLE_PREDICATE}",
        (evenement_id,),
        role=user.role,
    )
    notifies = 0
    for m in cibles:
        ctx = {"prenom": m.get("prenoms") or "", "titre": ev["titre"], "motif": payload.motif or ""}
        if notifier(str(m["id"]), user.role, "activite_annulee", ctx, ref_id=evenement_id, dedup=True):
            notifies += 1
    audit.log(user.id, user.role, "annulation_evenement", "evenement", evenement_id, {"motif": payload.motif, "notifies": notifies, "portee": portee, "activites": len(ids)})
    return _lire_evenement(evenement_id, user.role)


@router.get("/evenements/{evenement_id}/pieces", response_model=list[evenement_pieces.PieceEvenementOut])
def lister_pieces_evenement(
    evenement_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))],
) -> list[evenement_pieces.PieceEvenementOut]:
    """Attachments of an activity, shared with the collaboration app (same helper)."""
    return evenement_pieces.lister_pieces(str(evenement_id), user.role)


@router.post("/evenements/{evenement_id}/pieces", response_model=evenement_pieces.PieceEvenementOut, status_code=status.HTTP_201_CREATED)
def ajouter_piece_evenement(
    evenement_id: uuid.UUID,
    payload: evenement_pieces.PieceEvenementIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> evenement_pieces.PieceEvenementOut:
    piece = evenement_pieces.ajouter_piece(str(evenement_id), payload, user.id, user.role)
    audit.log(user.id, user.role, "ajout_piece_evenement", "evenement", str(evenement_id), {"nom": payload.nom})
    return piece


@router.delete("/evenements/pieces/{piece_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_piece_evenement(
    piece_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> None:
    info = evenement_pieces.supprimer_piece(str(piece_id), user.role)
    if info:
        audit.log(user.id, user.role, "suppression_piece_evenement", "evenement", info["evenement_id"], {"nom": info["nom"]})


@router.post("/evenements/{evenement_id}/reactiver", response_model=EvenementOut)
def reactiver_evenement(
    evenement_id: str,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
    portee: str = Query(default="cette_occurrence", pattern="^(cette_occurrence|toute_la_serie)$"),
) -> EvenementOut:
    """Re-activate a cancelled activity, or the whole series with portee='toute_la_serie'."""
    if not db.fetch_one("SELECT id FROM evenement WHERE id = %s", (evenement_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    ids = _serie_ids(evenement_id, portee, user.role)
    for eid in ids:
        db.execute(
            "UPDATE evenement SET annule = false, annule_le = NULL, annule_motif = NULL WHERE id = %s",
            (eid,),
            role=user.role,
        )
    audit.log(user.id, user.role, "reactivation_evenement", "evenement", evenement_id, {"portee": portee, "activites": len(ids)})
    return _lire_evenement(evenement_id, user.role)


def _activite_a_des_donnees(eid: str, role: str | None) -> bool:
    """True when an activity carries attendance, participation or survey answers."""
    n = db.fetch_one(
        "SELECT (SELECT count(*) FROM presence WHERE evenement_id = %s) "
        "+ (SELECT count(*) FROM participation WHERE evenement_id = %s) "
        "+ (SELECT count(*) FROM reponse_questionnaire rq JOIN questionnaire q ON q.id = rq.questionnaire_id WHERE q.evenement_id = %s) AS n",
        (eid, eid, eid),
        role=role,
    )
    return bool(n and int(n["n"]) > 0)


@router.delete("/evenements/{evenement_id}")
def delete_evenement(
    evenement_id: str,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
    portee: str = Query(default="cette_occurrence", pattern="^(cette_occurrence|toute_la_serie)$"),
) -> dict[str, int]:
    """Delete an activity, or the whole series with portee='toute_la_serie'.

    An activity that already carries attendance or survey responses is preserved
    (cancel it instead): a single delete on such an activity is refused (409); a
    series delete removes the empty occurrences and keeps the ones with data,
    reporting how many were deleted and how many were kept."""
    if not db.fetch_one("SELECT id FROM evenement WHERE id = %s", (evenement_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    if portee != "toute_la_serie" and _activite_a_des_donnees(evenement_id, user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette activité a des présences ou réponses enregistrées : annulez-la plutôt que de la supprimer")
    supprimees = 0
    conservees = 0
    for eid in _serie_ids(evenement_id, portee, user.role):
        if _activite_a_des_donnees(eid, user.role):
            conservees += 1
            continue
        # Remove the (empty) dependent rows first so no foreign key blocks the deletion.
        for table in ("comptage_volet_b", "questionnaire", "evenement_tag"):
            db.execute(f"DELETE FROM {table} WHERE evenement_id = %s", (eid,), role=user.role)
        db.execute("DELETE FROM evenement WHERE id = %s", (eid,), role=user.role)
        supprimees += 1
    audit.log(user.id, user.role, "suppression_evenement", "evenement", evenement_id, {"portee": portee, "supprimees": supprimees, "conservees": conservees})
    return {"supprimees": supprimees, "conservees": conservees}
