"""Back-office administration endpoints, served from the real PostgreSQL.

Every query sets the caller role for the per-role RLS policies (ADR-0002), which
act as defense in depth: the backend connects as the table owner, which bypasses
RLS, so access is enforced first by ``require_roles`` (which rejects unauthorized
callers before touching the database) and by the explicit ``WHERE`` scoping of
each query, not by RLS alone.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import audit, db, identifiants, identite
from .mappers import MEMBRE_PROFILE_FROM, MEMBRE_PROFILE_SELECT, membre_row_to_profile
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


@router.get("/membres", response_model=list[MembreProfile])
def list_membres(
    user: Annotated[UserMe, Depends(require_permission("membres.gerer"))],
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MembreProfile]:
    params: list[object] = []
    where = ""
    if q:
        where = (
            "WHERE m.nom ILIKE %s OR m.prenoms ILIKE %s "
            "OR m.matricule ILIKE %s OR m.email ILIKE %s"
        )
        # Escape LIKE metacharacters so a literal % or _ typed in the search is not
        # treated as a wildcard (default backslash escape).
        q_esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_esc}%"
        params.extend([like, like, like, like])
    params.extend([limit, offset])
    rows = db.fetch_all(
        f"""
        SELECT {MEMBRE_PROFILE_SELECT}
        {MEMBRE_PROFILE_FROM}
        {where}
        ORDER BY m.matricule ASC
        LIMIT %s OFFSET %s
        """,
        tuple(params),
        role=user.role,
    )
    return [membre_row_to_profile(r) for r in rows]


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
               CASE e.cible_type
                 WHEN 'coordination' THEN (SELECT nom FROM coordination WHERE id = e.cible_id)
                 WHEN 'commission' THEN (SELECT nom FROM commission WHERE id = e.cible_id)
                 WHEN 'intendance' THEN (SELECT nom FROM intendance WHERE id = e.cible_id)
                 WHEN 'tribu' THEN (SELECT nom FROM tribu WHERE id = e.cible_id)
                 ELSE NULL
               END AS cible_libelle
        FROM evenement e
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
    )


@router.post("/evenements", response_model=EvenementOut, status_code=status.HTTP_201_CREATED)
def create_evenement(
    payload: CreateEvenement,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> EvenementOut:
    liens = [x.strip() for x in payload.liens if x and x.strip()]
    primary = payload.lien_session or (liens[0] if liens else None)
    if primary and primary not in liens:
        liens = [primary, *liens]
    # Targeting: a general event carries no unit; a targeted one must name an
    # existing unit of the chosen kind, so an event can never point at nothing.
    cible_id: str | None = None
    if payload.cible_type != "general":
        if not payload.cible_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id required for a targeted event")
        table = {"coordination": "coordination", "commission": "commission", "intendance": "intendance", "tribu": "tribu"}[payload.cible_type]
        unit = db.fetch_one(f"SELECT id FROM {table} WHERE id = %s", (payload.cible_id,), role=user.role)
        if not unit:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown target unit")
        cible_id = payload.cible_id
    created = db.execute(
        """
        INSERT INTO evenement (titre, type, volet, debut, fin, lieu, mode, lien_session, liens, type_diffusion, visibilite, cible_type, cible_id, fenetre_reponse_heures, fuseau_horaire, cree_par)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, titre, type, volet, debut, fin, lieu, mode, session_ouverte,
                  lien_session, liens, type_diffusion, visibilite, cible_type, cible_id
        """,
        (
            payload.titre, payload.type, payload.volet, payload.debut, payload.fin, payload.lieu,
            payload.mode, primary, json.dumps(liens), payload.type_diffusion, payload.visibilite,
            payload.cible_type, cible_id, payload.fenetre_reponse_heures, payload.fuseau_horaire, user.id,
        ),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="insert failed")
    audit.log(
        user.id, user.role, "creation_evenement", "evenement", str(created["id"]),
        {"titre": created["titre"], "cible_type": payload.cible_type},
    )
    return _evenement_out(created)
