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

from . import audit, db, identifiants
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


def _temp_password() -> str:
    # Readable temporary password (avoids ambiguous characters).
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _send_temp_password(email: str, temp: str) -> tuple[bool, str]:
    """Send the temporary password. Returns (sent, provider) so the caller can
    surface a delivery failure instead of assuming success."""
    from .email_templates import render_temp_password_email

    text = (
        f"Bonjour,\n\nVotre compte ADSUM a été créé. Mot de passe temporaire : {temp}\n"
        f"Il est valable {TEMP_VALID_HOURS} heures. Connectez-vous à l'espace membre, "
        f"changez votre mot de passe puis complétez votre inscription.\n\n"
        f"Passé ce délai, contactez l'administration pour un nouveau mot de passe."
    )
    html = render_temp_password_email(temp, validity=f"{TEMP_VALID_HOURS} heures")
    return send_email(email, "ADSUM, votre accès et mot de passe temporaire", text, html)


def _temp_password_via_telegram(membre_id: str, role: str, temp: str) -> bool:
    """Also deliver the temporary password over Telegram when the member linked
    that channel. Some members use Telegram rather than e-mail, so the same
    credential reaches them on their default channel. Best-effort: never raises."""
    try:
        from . import channels

        member = db.fetch_one(
            "SELECT telegram_chat_id, langue, prenoms FROM membre WHERE id = %s",
            (membre_id,),
            role=role,
        )
        if not member or not member.get("telegram_chat_id"):
            return False
        prenom = (str(member.get("prenoms") or "").split(" ")[0]) or ""
        if member.get("langue") == "en":
            titre = "Your ADSUM access"
            corps = (
                f"Hello {prenom}, your ADSUM account was created. Temporary password: {temp}. "
                f"It is valid for {TEMP_VALID_HOURS} hours. Sign in to the member space, change your "
                f"password, then complete your registration. Do not share this password."
            )
        else:
            titre = "Votre accès ADSUM"
            corps = (
                f"Bonjour {prenom}, votre compte ADSUM a été créé. Mot de passe temporaire : {temp}. "
                f"Il est valable {TEMP_VALID_HOURS} heures. Connectez-vous à l'espace membre, changez "
                f"votre mot de passe, puis complétez votre inscription. Ne communiquez ce mot de passe à personne."
            )
        return channels.send_telegram(str(member["telegram_chat_id"]), channels.Message(titre=titre, corps_text=corps))
    except Exception:  # noqa: BLE001 - Telegram is a best-effort second channel
        return False


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
    matricule = identifiants.next_matricule(user.role)
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
    telegram_envoye = _temp_password_via_telegram(membre_id, user.role, temp)
    audit.log(user.id, user.role, "creation_inscription", "membre", membre_id,
              {"matricule": matricule, "email_envoye": sent, "canal": provider, "telegram_envoye": telegram_envoye})
    return {
        "membre_id": membre_id,
        "matricule": matricule,
        "expire_le": expire.isoformat(),
        "email_envoye": sent,
        "canal_email": provider,
        "telegram_envoye": telegram_envoye,
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
    telegram_envoye = _temp_password_via_telegram(membre_id, user.role, temp)
    audit.log(user.id, user.role, "relance_mdp_temporaire", "membre", membre_id,
              {"email_envoye": sent, "telegram_envoye": telegram_envoye})
    return {"ok": True, "expire_le": expire.isoformat(), "email_envoye": sent, "canal_email": provider, "telegram_envoye": telegram_envoye}


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
    champs_cibles: list[str] | None = None  # fields the member must correct (targeted correction)


@router.post("/admin/inscriptions/{membre_id}/decision")
def decision_inscription(membre_id: str, payload: DecisionIn, user: Annotated[UserMe, Depends(require_reviewer)]) -> dict[str, object]:
    if payload.decision not in DECISIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown decision")
    verifie = payload.decision == "approuve"
    # On a correction request, record which fields the member must fix; clear it
    # otherwise. The member's data stays intact (it lives on the membre row).
    cibles = payload.champs_cibles if payload.decision == "modification_demandee" else None
    db.execute(
        "UPDATE membre SET statut_inscription = %s, motif_refus = %s, champs_a_corriger = %s, "
        "decision_le = now(), decision_par = %s, verifie = CASE WHEN %s THEN true ELSE verifie END WHERE id = %s",
        (payload.decision, payload.motif, cibles, user.id, verifie, membre_id),
        role=user.role,
    )
    messages = {
        "approuve": ("Inscription validée", "Votre inscription est validée. Votre carte et votre QR sont actifs."),
        "refuse": ("Inscription refusée", f"Votre inscription a été refusée. Motif : {payload.motif or 'non précisé'}."),
        "modification_demandee": ("Modification demandée", f"L'administration demande une correction : {payload.motif or 'voir détails'}. Vos informations sont conservées : rouvrez votre dossier, corrigez uniquement ce qui est demandé, puis renvoyez-le."),
        "en_revue": ("Dossier en cours d'examen", "Votre dossier est en cours d'examen par l'administration."),
    }
    titre, corps = messages[payload.decision]
    # Multi-channel delivery (in-app + e-mail + Telegram), not in-app only.
    from . import channels

    mrow = db.fetch_one("SELECT prenoms FROM membre WHERE id = %s", (membre_id,), role=user.role)
    prenom = (str(mrow.get("prenoms") or "").split(" ")[0]) if mrow else "cher membre"
    corps_complet = f"Bonjour {prenom},\n\n{corps}"
    channels.dispatch(membre_id, user.role, channels.Message(titre=titre, corps_text=corps_complet, type_notif="inscription"))
    # On approval, open a hand-signed attestation task when the member's country
    # requires it (country legal matrix), with a one-month deadline.
    if payload.decision == "approuve":
        from .matrice_pays import ouvrir_attestation_si_besoin
        from .retention import set_retention

        ouvrir_attestation_si_besoin(membre_id, user.role)
        # Start the data-retention window (kept, not deleted) on approval.
        set_retention(membre_id, user.role)
    audit.log(user.id, user.role, "decision_inscription", "membre", membre_id, {"decision": payload.decision})
    return {"ok": True, "statut": payload.decision}


@router.get("/admin/inscriptions/{membre_id}/corrections")
def historique_corrections(membre_id: str, user: Annotated[UserMe, Depends(require_reviewer)]) -> list[dict[str, object]]:
    """Old/new/who/when trail of a member's corrections, for fast admin re-review."""
    rows = db.fetch_all(
        "SELECT champ, ancienne_valeur, nouvelle_valeur, modifie_le FROM correction_historique "
        "WHERE membre_id = %s ORDER BY modifie_le DESC",
        (membre_id,),
        role=user.role,
    )
    return [
        {
            "champ": r["champ"],
            "ancienne_valeur": r["ancienne_valeur"],
            "nouvelle_valeur": r["nouvelle_valeur"],
            "modifie_le": r["modifie_le"].isoformat() if r["modifie_le"] else None,
        }
        for r in rows
    ]


@router.get("/admin/inscriptions/{membre_id}/dossier")
def dossier_inscription(membre_id: str, user: Annotated[UserMe, Depends(require_reviewer)]) -> dict[str, object]:
    """Full review dossier for a member: identity photo, uploaded documents (each
    with a short-lived signed URL) and the electronic-signature proof.

    A reviewer must inspect the real evidence, the photo and the signed documents,
    before approving, instead of deciding blind on a name and a counter. Reads run
    as an owner query (the endpoint is already gated by require_reviewer) so every
    authorised reviewer sees the same dossier regardless of the per-table RLS.
    """
    from . import storage
    from .config import settings
    from .consentement import _signature_couvre_bloquants

    membre = db.fetch_one(
        "SELECT id, matricule, prenoms, nom, email, telephone, pays, ville, "
        "statut_inscription, verifie, photo_url FROM membre WHERE id = %s",
        (membre_id,),
        role=None,
    )
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")

    photo_signed = None
    if membre.get("photo_url"):
        try:
            photo_signed = storage.signed_download_url(settings.storage_bucket_photos, str(membre["photo_url"]))
        except storage.StorageError:
            photo_signed = None

    documents = []
    for d in db.fetch_all(
        "SELECT id, type, statut, nom_fichier, mime, bucket, chemin_stockage, chiffre, recu_le "
        "FROM document WHERE membre_id = %s ORDER BY recu_le DESC NULLS LAST",
        (membre_id,),
        role=None,
    ) or []:
        doc_id = str(d["id"])
        chiffre = bool(d.get("chiffre"))
        url = None
        content_path = None
        if chiffre:
            # Encrypted: read through the audited decrypting endpoint, not a signed URL.
            content_path = f"/api/v1/admin/documents/{doc_id}/content"
        elif d.get("chemin_stockage"):
            try:
                url = storage.signed_download_url(str(d["bucket"] or settings.storage_bucket_documents), str(d["chemin_stockage"]))
            except storage.StorageError:
                url = None
        documents.append({
            "id": doc_id,
            "type": d["type"],
            "statut": d["statut"],
            "nom_fichier": d.get("nom_fichier"),
            "mime": d.get("mime"),
            "recu_le": d["recu_le"].isoformat() if d.get("recu_le") else None,
            "url": url,
            "chiffre": chiffre,
            "content_path": content_path,
        })

    engagements = [
        {
            "type": e["type"],
            "version": e["version"],
            "signe_le": e["signe_le"].isoformat() if e.get("signe_le") else None,
            "canal": e.get("canal"),
        }
        for e in db.fetch_all(
            "SELECT type, version, signe_le, canal FROM engagement "
            "WHERE membre_id = %s AND code_verifie = true ORDER BY signe_le DESC",
            (membre_id,),
            role=None,
        ) or []
    ]
    preuves = [
        {
            "id": str(s["id"]),
            "signe_le": s["signe_le"].isoformat() if s.get("signe_le") else None,
            "hash_preuve": s.get("hash_preuve"),
            "canal": s.get("canaux"),
        }
        for s in db.fetch_all(
            "SELECT id, signe_le, hash_preuve, canaux FROM signature_engagement "
            "WHERE membre_id = %s AND code_verifie = true ORDER BY signe_le DESC",
            (membre_id,),
            role=None,
        ) or []
    ]
    return {
        "membre": {
            "id": str(membre["id"]),
            "matricule": membre.get("matricule"),
            "prenoms": membre.get("prenoms"),
            "nom": membre.get("nom"),
            "email": membre.get("email"),
            "telephone": membre.get("telephone"),
            "pays": membre.get("pays"),
            "ville": membre.get("ville"),
            "statut_inscription": membre.get("statut_inscription"),
            "verifie": bool(membre.get("verifie")),
        },
        "photo_url": photo_signed,
        "documents": documents,
        "signature": {
            "signe": _signature_couvre_bloquants(membre_id, None),
            "engagements": engagements,
            "preuves": preuves,
        },
    }


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
    en_correction = row["statut_inscription"] == "modification_demandee"
    incomplet = row["statut_inscription"] in ("incomplet", "modification_demandee")
    if incomplet:
        # On a correction cycle, keep an old/new trail before overwriting so the
        # admin can see exactly what changed (data itself is preserved, only the
        # requested fields are touched).
        before = {}
        if en_correction:
            cols = ", ".join(fields)
            before = db.fetch_one(f"SELECT {cols} FROM membre WHERE id = %s", (membre_id,), role=role) or {}
        sets = ", ".join(f"{k} = %s" for k in fields)
        db.execute(f"UPDATE membre SET {sets} WHERE id = %s", (*fields.values(), membre_id), role=role)
        if en_correction:
            for k, v in fields.items():
                old = before.get(k)
                if str(old) != str(v):
                    db.execute(
                        "INSERT INTO correction_historique (membre_id, champ, ancienne_valeur, nouvelle_valeur, modifie_par, modifie_le) "
                        "VALUES (%s, %s, %s, %s, %s, now())",
                        (membre_id, k, None if old is None else str(old), None if v is None else str(v), membre_id),
                        role=role,
                    )
        return {"ok": True, "updated": list(fields)}
    # Verified member: the edit belongs to the single admin-opened submission
    # cycle. Route it through the unified, idempotent submission so even a direct
    # PATCH cannot bypass the one-submission-per-unlock rule.
    return _soumettre_cycle(membre_id, role, fields, inclure_photo=False)


class SoumettreModif(BaseModel):
    """Payload of the single member submission: the edited text fields, and a
    hint that a replacement photo was staged (correctness never depends on the
    hint, only the confirmation wording does)."""

    champs: dict[str, str] = {}
    inclure_photo: bool = False


@router.post("/membres/me/modifications/soumettre")
def soumettre_modifications(
    payload: SoumettreModif, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]
) -> dict[str, object]:
    """One and only business submission for an admin-opened modification cycle.

    Gathers the edited text fields AND any staged replacement photo into a single
    proposal, consumes the whole unlock and moves the request to 'en_validation'.
    Idempotent: a refresh, a double click, a second tab or a direct API retry all
    find the cycle already submitted and get a clean 409."""
    membre_id, role = ctx
    return _soumettre_cycle(membre_id, role, dict(payload.champs), payload.inclure_photo)


def _soumettre_cycle(
    membre_id: str, role: str, fields: dict[str, object], inclure_photo: bool
) -> dict[str, object]:
    """Single, idempotent submission of an admin-opened modification cycle.

    The open cycle is the member's request in 'attente_membre' together with the
    unlocked fields. The status flip to 'en_validation' is the atomic lock: only
    the first caller flips it, so any replay hits a request that is no longer
    'attente_membre' and is rejected. The whole unlock is then consumed, so
    nothing stays editable until the administration opens a new cycle. The
    ``inclure_photo`` hint only shapes the confirmation wording; a staged photo is
    part of the cycle whenever one exists and 'photo_identite' was unlocked."""
    etat = db.fetch_one(
        "SELECT coalesce(champs_deverrouilles, '{}') AS deverrouilles, photo_pending_url "
        "FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not etat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    unlocked = set(etat.get("deverrouilles") or [])
    photo_incluse = bool(etat.get("photo_pending_url")) and "photo_identite" in unlocked
    cycle = db.fetch_one(
        "SELECT id FROM demande WHERE membre_id = %s AND statut = 'attente_membre' "
        "ORDER BY maj_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    if not unlocked or not cycle:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucun cycle de modification ouvert. L'administration doit débloquer les éléments à corriger.",
        )
    demande_id = str(cycle["id"])
    fields = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS}
    forbidden = [k for k in fields if k not in unlocked]
    if forbidden:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"locked_fields": forbidden})
    if not fields and not photo_incluse:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune modification à soumettre.")
    # ATOMIC LOCK: flip the cycle. A single statement, its own transaction: only
    # the first caller gets a row back, every replay gets none and is refused.
    locked = db.execute(
        "UPDATE demande SET statut = 'en_validation', echeance_reponse = NULL, maj_le = now() "
        "WHERE id = %s AND statut = 'attente_membre' RETURNING id",
        (demande_id,),
        role=role,
    )
    if not locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cette demande a déjà été soumise. Une seule soumission est possible par déblocage.",
        )
    if fields:
        before = db.fetch_one(f"SELECT {', '.join(fields)} FROM membre WHERE id = %s", (membre_id,), role=role) or {}
        try:
            db.execute(
                "INSERT INTO modification_membre (membre_id, demande_id, valeurs, valeurs_avant) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
                (membre_id, demande_id, json.dumps(fields, default=str), json.dumps(dict(before), default=str)),
                role=role,
            )
        except psycopg.errors.UniqueViolation as exc:  # pragma: no cover - guarded by the atomic flip above
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Cette demande a déjà été soumise."
            ) from exc
    # Consume the whole unlock: no field stays editable until a new admin cycle.
    db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=role)
    from .demandes import _notify_ticket, _system_message

    quoi = []
    if fields:
        quoi.append("informations")
    if photo_incluse:
        quoi.append("photo d'identité")
    detail = " et ".join(quoi) if quoi else "modifications"
    _system_message(
        demande_id, role,
        f"Le membre a soumis ses {detail} pour validation. En attente de validation finale par l'administration.",
    )
    _notify_ticket(
        demande_id, role, "Modification soumise",
        f"Vos {detail} ont été transmises. Elles seront enregistrées après validation par l'administration.",
    )
    return {"ok": True, "pending_validation": True, "champs": list(fields), "photo": photo_incluse}


@router.get("/membres/me/inscription")
def mon_inscription(ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    membre_id, role = ctx
    row = db.fetch_one(
        "SELECT statut_inscription, motif_refus, champs_a_corriger, soumis_le, decision_le, verifie FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    return {
        "statut": row["statut_inscription"],
        "motif_refus": row["motif_refus"],
        "champs_a_corriger": list(row["champs_a_corriger"] or []),
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
    # A verified electronic signature covering every active blocking consent
    # document is required on top of the profile and document gates. Imported
    # locally to avoid a circular import (consentement imports from this module).
    from .consentement import _signature_couvre_bloquants

    signe = _signature_couvre_bloquants(membre_id, role)
    if missing or (docs and int(docs["n"]) == 0) or not signe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "missing_fields": missing,
                "needs_document": bool(docs and int(docs["n"]) == 0),
                "needs_signature": not signe,
            },
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
