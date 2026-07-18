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
from pydantic import BaseModel, Field, field_validator

from . import audit, db, identifiants
from .auth import current_user
from .email_gateway import send_email
from .fields import LineStr, ShortStr
from .modifications import _EDITABLE_FIELDS, soumettre_cycle
from .permissions_rbac import require_permission
from .schemas import UserMe
from .security import hash_password

router = APIRouter(prefix="/api/v1", tags=["inscription"])

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
# The single and bulk creation endpoints live in app.inscription_admin; this module
# keeps the shared creation helper they both call.


def creer_membre_inscription(email: str, prenoms: str | None, nom: str | None, actor: UserMe) -> dict[str, object]:
    """Create one member registration (member row + login) and send the access.

    Shared by the single and bulk endpoints. Raises HTTP 409 on a duplicate e-mail so
    the bulk caller counts it without aborting the batch. Starts at 'incomplet'.
    """
    matricule = identifiants.next_matricule(actor.role, nom, prenoms)
    try:
        created = db.execute(
            "INSERT INTO membre (matricule, email, prenoms, nom, statut, verifie, statut_inscription) "
            "VALUES (%s, %s, %s, %s, 'actif', false, 'incomplet') RETURNING id",
            (matricule, email, prenoms, nom),
            role=actor.role,
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
            (email, hash_password(temp), membre_id, expire),
            role=actor.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="account already exists") from exc
    sent, provider = _send_temp_password(email, temp)
    telegram_envoye = _temp_password_via_telegram(membre_id, actor.role, temp)
    if not sent and not telegram_envoye:
        # The member received their access on no channel: record it in the delivery
        # failure ledger so the administration sees it and re-sends the credentials,
        # instead of a member silently stuck at the very first step.
        from . import channels

        channels._record_echec(membre_id, actor.role, "compte_cree", "email", "envoi du mot de passe temporaire echoue (aucun canal)")
    audit.log(actor.id, actor.role, "creation_inscription", "membre", membre_id,
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
def relancer_mdp(membre_id: str, user: Annotated[UserMe, Depends(require_permission("inscriptions.administrer"))]) -> dict[str, object]:
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
    if not sent and not telegram_envoye:
        from . import channels

        channels._record_echec(membre_id, user.role, "compte_cree", "email", "relance du mot de passe temporaire echouee (aucun canal)")
    audit.log(user.id, user.role, "relance_mdp_temporaire", "membre", membre_id,
              {"email_envoye": sent, "telegram_envoye": telegram_envoye})
    return {"ok": True, "expire_le": expire.isoformat(), "email_envoye": sent, "canal_email": provider, "telegram_envoye": telegram_envoye}


# --- Admin: review and decision -------------------------------------------
# The registration lists (one queue per lifecycle stage) and their counts live in
# app.inscription_admin to keep this module focused and under the size threshold.


class DecisionIn(BaseModel):
    decision: str
    motif: str | None = None
    champs_cibles: list[str] | None = None  # fields the member must correct (targeted correction)


@router.post("/admin/inscriptions/{membre_id}/decision")
def decision_inscription(membre_id: str, payload: DecisionIn, user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))]) -> dict[str, object]:
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
    # If the correction targets the identity photo, unlock 'photo_identite' so the
    # member can actually replace it: without the unlock, photo confirm returns 403
    # and the member is asked to re-send a photo they are not allowed to change.
    if cibles and any(c in ("photo", "photo_identite") for c in cibles):
        db.execute(
            "UPDATE membre SET champs_deverrouilles = array("
            "  SELECT DISTINCT e FROM unnest(coalesce(champs_deverrouilles, '{}'::text[]) || ARRAY['photo_identite']::text[]) AS e"
            ") WHERE id = %s",
            (membre_id,),
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
def historique_corrections(membre_id: str, user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))]) -> list[dict[str, object]]:
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
def dossier_inscription(membre_id: str, user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))]) -> dict[str, object]:
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
        "SELECT m.id, m.matricule, m.code_membre, m.prenoms, m.nom, m.nom_affiche, m.email, "
        "m.telephone, m.indicatif_telephone, m.date_naissance, m.genre, m.pays, m.ville, m.region, "
        "m.adresse, m.profession, m.date_entree, m.promotion, m.berger_declare, m.berger_nom_declare, "
        "m.statut, m.statut_inscription, m.verifie, m.type_membre, "
        "m.photo_url, m.cree_le, m.groupe, ne.libelle AS niveau_libelle, "
        "c.nom AS commission, co.nom AS coordination, i.nom AS intendance, t.nom AS tribu "
        "FROM membre m "
        "LEFT JOIN niveau_engagement ne ON ne.cle = m.type_membre "
        "LEFT JOIN commission c ON c.id = m.commission_id "
        "LEFT JOIN coordination co ON co.id = m.coordination_id "
        "LEFT JOIN intendance i ON i.id = m.intendance_id "
        "LEFT JOIN tribu t ON t.id = m.tribu_id "
        "WHERE m.id = %s",
        (membre_id,),
        role=None,
    )
    if not membre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")

    fonctions = [
        {"libelle": f.get("libelle_h"), "perimetre": f.get("perimetre"), "confirmee": bool(f.get("confirmee")),
         "categorie": f.get("categorie") or "fonction"}
        for f in db.fetch_all(
            "SELECT fh.libelle_h, fh.categorie, mf.perimetre, mf.confirmee FROM membre_fonction mf "
            "JOIN fonction_honorifique fh ON fh.cle = mf.fonction_cle "
            "WHERE mf.membre_id = %s AND mf.actif = true "
            "ORDER BY CASE fh.categorie WHEN 'fonction_speciale' THEN 0 WHEN 'titre' THEN 1 WHEN 'fonction' THEN 2 ELSE 3 END, mf.principale DESC, mf.ordre",
            (membre_id,),
            role=None,
        ) or []
    ]

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
            "code_membre": membre.get("code_membre"),
            "prenoms": membre.get("prenoms"),
            "nom": membre.get("nom"),
            "nom_affiche": membre.get("nom_affiche"),
            "email": membre.get("email"),
            "telephone": membre.get("telephone"),
            "indicatif_telephone": membre.get("indicatif_telephone"),
            "date_naissance": membre["date_naissance"].isoformat() if membre.get("date_naissance") else None,
            "genre": membre.get("genre"),
            "pays": membre.get("pays"),
            "ville": membre.get("ville"),
            "region": membre.get("region"),
            "adresse": membre.get("adresse"),
            "profession": membre.get("profession"),
            "date_entree": membre["date_entree"].isoformat() if membre.get("date_entree") else None,
            "promotion": membre.get("promotion"),
            # Self-declared shepherd status: shown to the reviewer so the
            # administration can grant (or not) the real est_berger flag.
            "berger_declare": bool(membre.get("berger_declare")),
            "berger_nom_declare": membre.get("berger_nom_declare"),
            "commission": membre.get("commission"),
            "sous_commission": membre.get("groupe"),
            "coordination": membre.get("coordination"),
            "intendance": membre.get("intendance"),
            "tribu": membre.get("tribu"),
            "niveau": membre.get("niveau_libelle"),
            "fonctions": fonctions,
            "statut": membre.get("statut"),
            "statut_inscription": membre.get("statut_inscription"),
            "verifie": bool(membre.get("verifie")),
            "cree_le": membre["cree_le"].isoformat() if membre.get("cree_le") else None,
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


class FonctionSouhaitee(BaseModel):
    """One attribution the member declares at registration (special function,
    function or particular function), to be confirmed by the administration. The
    consecration title is handled apart, through ``berger_declare``."""

    cle: str = Field(min_length=2, max_length=40, pattern="^[a-z0-9_]+$")
    perimetre: str | None = Field(default=None, max_length=120)


class ProfilUpdate(BaseModel):
    prenoms: LineStr | None = None
    nom: LineStr | None = None
    telephone: ShortStr | None = None
    indicatif_telephone: ShortStr | None = None
    date_naissance: ShortStr | None = None
    naissance_annee_visible: bool | None = None
    genre: str | None = Field(default=None, max_length=120, pattern="^(homme|femme|autre)$")
    pays: LineStr | None = None
    region: LineStr | None = None
    ville: LineStr | None = None
    adresse: LineStr | None = None
    adresse_complement: LineStr | None = None
    commission_id: ShortStr | None = None
    intendance_id: ShortStr | None = None
    coordination_id: ShortStr | None = None
    tribu_id: ShortStr | None = None
    groupe: LineStr | None = None
    profession: LineStr | None = None
    niveau_etudes: LineStr | None = None
    situation_matrimoniale: str | None = Field(default=None, max_length=120, pattern="^(celibataire|en_couple|fiance|marie|veuf|divorce)$")
    type_mariage: str | None = Field(default=None, max_length=120, pattern="^(dot|religieux|dot_et_religieux|civil)$")
    baptise: bool | None = None
    confirme: bool | None = None
    premiere_communion: bool | None = None
    type_membre: str | None = Field(default=None, max_length=120, pattern="^[a-z][a-z0-9_]{1,39}$")
    fonction_cle: ShortStr | None = None
    # Multi-category declarations at registration: the four form blocks (special
    # functions, functions, particular functions) each submit their selections
    # here, with an optional scope. Titles go through berger_declare, never here.
    fonctions_souhaitees: list[FonctionSouhaitee] | None = None
    # External member code in the wider organisation (distinct from the ADSUM
    # matricule): optional, uppercased, unique (partial index 0080), refused when
    # it collides with an existing matricule (the login resolver accepts both).
    code_membre: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z0-9\- ]*$")
    # Community journey facts the member declares themselves (also editable by
    # the administration in the back office).
    date_entree: ShortStr | None = None
    promotion: LineStr | None = None
    # Shepherd SELF-DECLARATION (to be confirmed): never grants est_berger by
    # itself; the administration reviews the dossier and grants the real flag.
    berger_declare: bool | None = None
    berger_nom_declare: LineStr | None = None
    # Civil identity completeness (mostly for married women): birth name, marital
    # name and which one is displayed. nom_affiche selects the shown family name.
    nom_naissance: LineStr | None = None
    nom_marital: LineStr | None = None
    nom_affiche: str | None = Field(default=None, max_length=20, pattern="^(naissance|marital)?$")
    # Extra contact channel and preferences the member can set at registration.
    whatsapp_numero: str | None = Field(default=None, max_length=32, pattern=r"^[+0-9 ().\-]*$")
    langue: str | None = Field(default=None, max_length=5, pattern="^(fr|en)?$")
    # Peer birthday directory opt-in (RGPD): whether the member appears in others'
    # birthday overlays. Defaults to visible; the member may opt out here.
    anniversaire_visible_annuaire: bool | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_to_none(cls, valeur: object) -> object:
        """Coerce empty or whitespace-only strings to None.

        A member who leaves an optional dropdown on "Selectionner..." sends an empty
        string. Written as-is that empty string would land in a uuid column
        (intendance_id, tribu_id, commission_id) or a CHECK-constrained enum column
        (situation_matrimoniale, type_membre) and raise a 500 at save time, blocking
        the whole submission. Normalising to None here makes those fields NULL, which
        is valid, so an unselected optional field never blocks a member.
        """
        if isinstance(valeur, str) and valeur.strip() == "":
            return None
        return valeur


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
    en_correction = row["statut_inscription"] == "modification_demandee"
    incomplet = row["statut_inscription"] in ("incomplet", "modification_demandee")
    # The member code is normalised (upper, empty -> NULL) and must never equal an
    # existing ADSUM matricule: the login resolver accepts both, so a collision
    # would make that identifier ambiguous. Same guard as the modification cycle.
    if "code_membre" in fields:
        code = str(fields["code_membre"] or "").strip().upper()
        fields["code_membre"] = code or None
        if code and db.fetch_one("SELECT 1 FROM membre WHERE upper(matricule) = upper(%s) LIMIT 1", (code,), role=role):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ce code membre correspond à un matricule ADSUM existant. Choisissez un autre code.",
            )
    # A declared shepherd name only makes sense with the declaration itself:
    # answering "no" clears any previously stored name so no orphan value stays.
    if fields.get("berger_declare") is False:
        fields["berger_nom_declare"] = None
    # During registration, every chosen attribution is a PROPOSAL: recorded as an
    # unconfirmed catalogue function (never the confirmed mirror), so the four form
    # blocks are no longer silently dropped and the administration keeps the sole
    # authority to confirm them (fonction_confirmee stays false until then). The
    # consecration TITLE is refused here on purpose: it flows through berger_declare.
    souhaitees: list[tuple[str, str | None]] = []
    if incomplet:
        if payload.fonction_cle:
            souhaitees.append((str(payload.fonction_cle).strip().lower(), None))
        for item in payload.fonctions_souhaitees or []:
            souhaitees.append((item.cle.strip().lower(), (item.perimetre or "").strip() or None))
    fonctions_proposees = 0
    for cle_souhaitee, perimetre in souhaitees:
        connue = db.fetch_one(
            "SELECT cle, categorie FROM fonction_honorifique WHERE cle = %s AND actif = true",
            (cle_souhaitee,), role=role,
        )
        # A title is never self-assigned through the function list (managed via the
        # consecration flag), and an unknown/inactive key is silently ignored.
        if not connue or (connue.get("categorie") == "titre"):
            continue
        db.execute(
            "INSERT INTO membre_fonction (membre_id, fonction_cle, perimetre, confirmee, actif, principale, ordre) "
            "SELECT %s, %s, %s, false, true, NOT EXISTS (SELECT 1 FROM membre_fonction WHERE membre_id = %s AND principale), 0 "
            "WHERE NOT EXISTS (SELECT 1 FROM membre_fonction WHERE membre_id = %s AND lower(fonction_cle) = %s)",
            (membre_id, str(connue["cle"]), perimetre, membre_id, membre_id, str(connue["cle"]).lower()),
            role=role,
        )
        fonctions_proposees += 1
    if not fields and not fonctions_proposees:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    if not fields:
        return {"ok": True, "updated": ["fonctions_souhaitees"]}
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
    # cycle. Route it through the unified, idempotent submission (in
    # modifications.py) so even a direct PATCH cannot bypass the
    # one-submission-per-unlock rule.
    return soumettre_cycle(membre_id, role, fields, inclure_photo=False)


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
        "SELECT prenoms, nom, telephone, date_naissance, genre, ville, pays, commission_id, tribu_id, "
        "coordination_id, intendance_id "
        "FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    # Server-side required-field gate before a registration can be submitted.
    required = ["prenoms", "nom", "telephone", "date_naissance", "genre", "ville", "pays", "commission_id", "tribu_id"]
    missing = [k for k in required if not row.get(k)]
    # Organizational axis: a member must belong to at least one of a coordination
    # or an intendance (both is allowed but rare). Reported as a single virtual
    # field so the member sees one clear "rattachement" item to fix.
    if not (row.get("coordination_id") or row.get("intendance_id")):
        missing.append("rattachement")
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
    # Idempotent transition: only a dossier still being filled (incomplet or in a
    # correction cycle) can move to 'soumis'. A double click, a browser retry or a
    # dossier already decided (valide/refuse) matches no row, so it never regresses
    # a decided dossier and never re-sends the acknowledgement.
    transition = db.execute(
        "UPDATE membre SET statut_inscription = 'soumis', soumis_le = now() "
        "WHERE id = %s AND statut_inscription IN ('incomplet', 'modification_demandee') RETURNING id",
        (membre_id,),
        role=role,
    )
    if not transition:
        courant = db.fetch_one("SELECT statut_inscription FROM membre WHERE id = %s", (membre_id,), role=role)
        return {"ok": True, "statut": (courant or {}).get("statut_inscription", "soumis"), "canaux": {}, "idempotent": True}
    # Multi-channel acknowledgement (in-app + e-mail + Telegram) so the member is
    # told, off-app too, that the dossier was received and will be reviewed. Sent
    # only on a real transition.
    from .notifications import notifier

    prenom = (str(row.get("prenoms") or "").split(" ")[0]) or "cher membre"
    canaux = notifier(membre_id, role, "inscription_soumise", {"prenom": prenom})
    return {"ok": True, "statut": "soumis", "canaux": canaux}
