"""Member registration workflow.

Admin creates an account from an e-mail; the system generates a 72h temporary
password and e-mails it. The member logs in, is forced to change the password
(with an e-mail OTP), completes the required profile, uploads the identity
pieces/photo/consents, then submits. The admin reviews and approves, rejects or
asks for a modification, with a tracked status the member follows in real time.
Backed by the 0013 schema. Reuses the existing e-mail gateway and audit log.
"""
# ruff: noqa: E501
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from . import audit, db
from .auth import current_user
from .deps import require_roles
from .email_gateway import send_email
from .schemas import UserMe
from .security import hash_password

router = APIRouter(prefix="/api/v1", tags=["inscription"])

require_writer = require_roles("super_admin", "admin")
require_reviewer = require_roles("super_admin", "admin", "gestionnaire")
TEMP_VALID_HOURS = 72
DECISIONS = {"approuve", "refuse", "modification_demandee", "en_revue"}


def _membre_ctx(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


def _next_matricule(role: str) -> str:
    row = db.fetch_one(
        "SELECT COALESCE(MAX(CAST(SUBSTRING(matricule FROM 5) AS integer)), 0) AS last "
        "FROM membre WHERE matricule ~ '^ADS-[0-9]{6}$'",
        (),
        role=role,
    )
    last = row["last"] if row else 0
    return f"ADS-{last + 1:06d}"


def _temp_password() -> str:
    # Readable temporary password (avoids ambiguous characters).
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _send_temp_password(email: str, temp: str) -> None:
    from .email_templates import render_temp_password_email

    text = (
        f"Bonjour,\n\nVotre compte ADSUM a ete cree. Mot de passe temporaire : {temp}\n"
        f"Il est valable {TEMP_VALID_HOURS} heures. Connectez-vous a l'espace membre, "
        f"changez votre mot de passe puis completez votre inscription.\n\n"
        f"Passe ce delai, contactez l'administration pour un nouveau mot de passe."
    )
    html = render_temp_password_email(temp, validity=f"{TEMP_VALID_HOURS} heures")
    send_email(email, "ADSUM, votre acces et mot de passe temporaire", text, html)


def _notify_membre(membre_id: str, role: str, titre: str, corps: str) -> None:
    db.execute(
        "INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le) VALUES (%s, 'inscription', %s, %s, false, now())",
        (membre_id, titre, corps),
        role=role,
    )


# --- Admin: account creation ----------------------------------------------

class CompteMembreIn(BaseModel):
    email: EmailStr
    prenoms: str | None = None
    nom: str | None = None


@router.post("/admin/inscriptions/membre", status_code=status.HTTP_201_CREATED)
def creer_compte_membre(payload: CompteMembreIn, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    matricule = _next_matricule(user.role)
    try:
        created = db.execute(
            "INSERT INTO membre (matricule, email, prenoms, nom, statut, verifie, statut_inscription) "
            "VALUES (%s, %s, %s, %s, 'actif', false, 'incomplet') RETURNING id",
            (matricule, str(payload.email), payload.prenoms, payload.nom),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email or matricule already in use") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="member not created")
    membre_id = str(created["id"])
    temp = _temp_password()
    expire = datetime.now(tz=UTC) + timedelta(hours=TEMP_VALID_HOURS)
    try:
        db.execute(
            "INSERT INTO utilisateur (email, hash_mdp, role, membre_id, actif, mdp_temporaire, mdp_expire_le, doit_changer_mdp) "
            "VALUES (%s, %s, 'membre', %s, true, true, %s, true)",
            (str(payload.email), hash_password(temp), membre_id, expire),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account already exists") from exc
    _send_temp_password(str(payload.email), temp)
    audit.log(user.id, user.role, "creation_inscription", "membre", membre_id, {"matricule": matricule})
    return {"membre_id": membre_id, "matricule": matricule, "expire_le": expire.isoformat()}


@router.post("/admin/inscriptions/{membre_id}/relancer-mdp")
def relancer_mdp(membre_id: str, user: Annotated[UserMe, Depends(require_writer)]) -> dict[str, object]:
    row = db.fetch_one("SELECT email FROM membre WHERE id = %s", (membre_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    temp = _temp_password()
    expire = datetime.now(tz=UTC) + timedelta(hours=TEMP_VALID_HOURS)
    db.execute(
        "UPDATE utilisateur SET hash_mdp = %s, mdp_temporaire = true, mdp_expire_le = %s, doit_changer_mdp = true "
        "WHERE membre_id = %s",
        (hash_password(temp), expire, membre_id),
        role=user.role,
    )
    _send_temp_password(str(row["email"]), temp)
    audit.log(user.id, user.role, "relance_mdp_temporaire", "membre", membre_id, {})
    return {"ok": True, "expire_le": expire.isoformat()}


# --- Admin: review and decision -------------------------------------------

@router.get("/admin/inscriptions")
def list_inscriptions(user: Annotated[UserMe, Depends(require_reviewer)]) -> list[dict[str, object]]:
    rows = db.fetch_all(
        "SELECT id, matricule, prenoms, nom, email, statut_inscription, soumis_le, "
        "(SELECT count(*) FROM document d WHERE d.membre_id = membre.id) AS nb_documents "
        "FROM membre WHERE statut_inscription IN ('soumis', 'en_revue', 'modification_demandee') "
        "ORDER BY soumis_le ASC NULLS LAST",
        (),
        role=user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "matricule": r["matricule"],
            "nom": f"{r['prenoms'] or ''} {r['nom'] or ''}".strip(),
            "email": r["email"],
            "statut": r["statut_inscription"],
            "soumis_le": r["soumis_le"].isoformat() if r["soumis_le"] else None,
            "nb_documents": int(r["nb_documents"]),
        }
        for r in rows
    ]


class DecisionIn(BaseModel):
    decision: str
    motif: str | None = None


@router.post("/admin/inscriptions/{membre_id}/decision")
def decision_inscription(membre_id: str, payload: DecisionIn, user: Annotated[UserMe, Depends(require_reviewer)]) -> dict[str, object]:
    if payload.decision not in DECISIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown decision")
    verifie = payload.decision == "approuve"
    db.execute(
        "UPDATE membre SET statut_inscription = %s, motif_refus = %s, decision_le = now(), decision_par = %s, "
        "verifie = CASE WHEN %s THEN true ELSE verifie END WHERE id = %s",
        (payload.decision, payload.motif, user.id, verifie, membre_id),
        role=user.role,
    )
    messages = {
        "approuve": ("Inscription validee", "Votre inscription est validee. Votre carte et votre QR sont actifs."),
        "refuse": ("Inscription refusee", f"Votre inscription a ete refusee. Motif : {payload.motif or 'non precise'}."),
        "modification_demandee": ("Modification demandee", f"L'administration demande une modification : {payload.motif or 'voir details'}."),
        "en_revue": ("Dossier en cours d'examen", "Votre dossier est en cours d'examen par l'administration."),
    }
    titre, corps = messages[payload.decision]
    _notify_membre(membre_id, user.role, titre, corps)
    audit.log(user.id, user.role, "decision_inscription", "membre", membre_id, {"decision": payload.decision})
    return {"ok": True, "statut": payload.decision}


# --- Member: status and submission ----------------------------------------

@router.get("/membres/me/inscription")
def mon_inscription(ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT statut_inscription, motif_refus, soumis_le, decision_le, verifie FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return {
        "statut": row["statut_inscription"],
        "motif_refus": row["motif_refus"],
        "soumis_le": row["soumis_le"].isoformat() if row["soumis_le"] else None,
        "decision_le": row["decision_le"].isoformat() if row["decision_le"] else None,
        "verifie": bool(row["verifie"]),
    }


@router.post("/membres/me/inscription/soumettre")
def soumettre_inscription(ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT prenoms, nom, telephone, date_naissance, genre, ville, pays, commission_id, tribu_id "
        "FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # Server-side required-field gate before a registration can be submitted.
    required = ["prenoms", "nom", "telephone", "date_naissance", "genre", "ville", "pays", "commission_id", "tribu_id"]
    missing = [k for k in required if not row.get(k)]
    docs = db.fetch_one("SELECT count(*) AS n FROM document WHERE membre_id = %s AND chemin_stockage IS NOT NULL", (membre_id,), role=role)
    if missing or (docs and int(docs["n"]) == 0):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"missing_fields": missing, "needs_document": bool(docs and int(docs["n"]) == 0)},
        )
    db.execute(
        "UPDATE membre SET statut_inscription = 'soumis', soumis_le = now() WHERE id = %s",
        (membre_id,),
        role=role,
    )
    _notify_membre(membre_id, role, "Inscription soumise", "Votre inscription a ete soumise. L'administration va l'examiner.")
    return {"ok": True, "statut": "soumis"}
