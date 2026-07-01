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

import json
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


def _send_temp_password(email: str, temp: str) -> tuple[bool, str]:
    """Send the temporary password. Returns (sent, provider) so the caller can
    surface a delivery failure instead of assuming success."""
    from .email_templates import render_temp_password_email

    text = (
        f"Bonjour,\n\nVotre compte ADSUM a ete cree. Mot de passe temporaire : {temp}\n"
        f"Il est valable {TEMP_VALID_HOURS} heures. Connectez-vous a l'espace membre, "
        f"changez votre mot de passe puis completez votre inscription.\n\n"
        f"Passe ce delai, contactez l'administration pour un nouveau mot de passe."
    )
    html = render_temp_password_email(temp, validity=f"{TEMP_VALID_HOURS} heures")
    return send_email(email, "ADSUM, votre acces et mot de passe temporaire", text, html)


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
    sent, provider = _send_temp_password(str(payload.email), temp)
    audit.log(user.id, user.role, "creation_inscription", "membre", membre_id,
              {"matricule": matricule, "email_envoye": sent, "canal": provider})
    return {
        "membre_id": membre_id,
        "matricule": matricule,
        "expire_le": expire.isoformat(),
        "email_envoye": sent,
        "canal_email": provider,
    }


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
    sent, provider = _send_temp_password(str(row["email"]), temp)
    audit.log(user.id, user.role, "relance_mdp_temporaire", "membre", membre_id, {"email_envoye": sent})
    return {"ok": True, "expire_le": expire.isoformat(), "email_envoye": sent, "canal_email": provider}


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
        "modification_demandee": ("Modification demandee", f"L'administration demande une correction : {payload.motif or 'voir details'}. Vos informations sont conservees : rouvrez votre dossier, corrigez uniquement ce qui est demande, puis renvoyez-le."),
        "en_revue": ("Dossier en cours d'examen", "Votre dossier est en cours d'examen par l'administration."),
    }
    titre, corps = messages[payload.decision]
    # Multi-channel delivery (in-app + e-mail + Telegram), not in-app only.
    from . import channels

    mrow = db.fetch_one("SELECT prenoms FROM membre WHERE id = %s", (membre_id,), role=user.role)
    prenom = (str(mrow.get("prenoms") or "").split(" ")[0]) if mrow else "cher membre"
    corps_complet = f"Bonjour {prenom},\n\n{corps}"
    channels.dispatch(membre_id, user.role, channels.Message(titre=titre, corps_text=corps_complet, type_notif="inscription"))
    audit.log(user.id, user.role, "decision_inscription", "membre", membre_id, {"decision": payload.decision})
    return {"ok": True, "statut": payload.decision}


# --- Member: status and submission ----------------------------------------

_EDITABLE_FIELDS = {
    "prenoms", "nom", "telephone", "indicatif_telephone", "date_naissance",
    "naissance_annee_visible", "genre", "pays", "region", "ville", "adresse",
    "adresse_complement", "commission_id", "intendance_id", "tribu_id", "groupe",
    "profession", "niveau_etudes", "situation_matrimoniale", "type_mariage",
    "baptise", "confirme", "premiere_communion", "type_membre", "fonction_cle",
}


class ProfilUpdate(BaseModel):
    prenoms: str | None = None
    nom: str | None = None
    telephone: str | None = None
    indicatif_telephone: str | None = None
    date_naissance: str | None = None
    naissance_annee_visible: bool | None = None
    genre: str | None = None
    pays: str | None = None
    region: str | None = None
    ville: str | None = None
    adresse: str | None = None
    adresse_complement: str | None = None
    commission_id: str | None = None
    intendance_id: str | None = None
    tribu_id: str | None = None
    groupe: str | None = None
    profession: str | None = None
    niveau_etudes: str | None = None
    situation_matrimoniale: str | None = None
    type_mariage: str | None = None
    baptise: bool | None = None
    confirme: bool | None = None
    premiere_communion: bool | None = None
    type_membre: str | None = None
    fonction_cle: str | None = None


@router.patch("/membres/me/profil")
def update_mon_profil(payload: ProfilUpdate, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    """Member self-edits their profile.

    During registration (statut incomplet), edits are written straight to the
    record because the whole dossier is validated afterwards by the admin. On a
    verified member, edits are only allowed on admin-unlocked fields and are not
    committed directly: they are stored as a pending proposal awaiting a final
    admin validation (see demandes.py), like a bank or a public administration.
    """
    membre_id, role = ctx
    row = db.fetch_one("SELECT statut_inscription, champs_deverrouilles FROM membre WHERE id = %s", (membre_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    fields = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if k in _EDITABLE_FIELDS}
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    incomplet = row["statut_inscription"] in ("incomplet", "modification_demandee")
    if incomplet:
        sets = ", ".join(f"{k} = %s" for k in fields)
        db.execute(f"UPDATE membre SET {sets} WHERE id = %s", (*fields.values(), membre_id), role=role)
        return {"ok": True, "updated": list(fields)}
    # Verified member: only admin-unlocked fields, and never a direct write.
    unlocked = set(row["champs_deverrouilles"] or [])
    forbidden = [k for k in fields if k not in unlocked]
    if forbidden:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"locked_fields": forbidden})
    return _propose_modification(membre_id, role, fields)


def _propose_modification(membre_id: str, role: str, fields: dict[str, object]) -> dict[str, object]:
    """Store the member's edits as a pending modification awaiting admin validation."""
    columns = ", ".join(fields)
    before = db.fetch_one(f"SELECT {columns} FROM membre WHERE id = %s", (membre_id,), role=role) or {}
    demande = db.fetch_one(
        "SELECT id FROM demande WHERE membre_id = %s AND type = 'modification_info' "
        "AND statut NOT IN ('resolue', 'refusee') ORDER BY maj_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    demande_id = str(demande["id"]) if demande else None
    db.execute(
        "INSERT INTO modification_membre (membre_id, demande_id, valeurs, valeurs_avant) "
        "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
        (membre_id, demande_id, json.dumps(fields, default=str), json.dumps(dict(before), default=str)),
        role=role,
    )
    if demande_id:
        db.execute("UPDATE demande SET statut = 'en_validation', maj_le = now() WHERE id = %s", (demande_id,), role=role)
    return {"ok": True, "pending_validation": True, "champs": list(fields)}


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
    # Multi-channel acknowledgement (in-app + e-mail + Telegram) so the member is
    # told, off-app too, that the dossier was received and will be reviewed.
    from .notifications import notifier

    prenom = (str(row.get("prenoms") or "").split(" ")[0]) or "cher membre"
    canaux = notifier(membre_id, role, "inscription_soumise", {"prenom": prenom})
    return {"ok": True, "statut": "soumis", "canaux": canaux}
