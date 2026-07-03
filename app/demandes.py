"""Member requests and conversation with the administration (interface like a
bank / CAF). A member opens a request (info change with reason, question…), and
exchanges messages with the staff; the staff can change the status and unlock the
specific member fields a justified request concerns. Backed by the 0011 schema.
"""
# ruff: noqa: E501 - single-line SQL statements are kept readable.
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db
from .auth import current_user
from .deps import require_roles
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["demandes"])

STAFF = ("super_admin", "admin", "gestionnaire")
require_staff = require_roles(*STAFF)
require_lecture = require_roles("super_admin", "admin", "gestionnaire", "direction")


def _require_membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role, user.email


class DemandeIn(BaseModel):
    type: str = "question"
    sujet: str
    champ_concerne: str | None = None
    message: str
    categorie: str | None = None
    sous_categorie: str | None = None
    # Optional supporting document: never required to submit (absolute rule).
    document_id: str | None = None


class MessageIn(BaseModel):
    corps: str
    # Optional supporting document uploaded through the standard document flow,
    # attached to this very ticket message (rule: files live inside the thread).
    document_id: str | None = None


class DemandePatch(BaseModel):
    statut: str | None = None
    champs_deverrouilles: list[str] | None = None
    motif: str | None = None


class PieceRequeteIn(BaseModel):
    description: str


class MessageOut(BaseModel):
    id: str
    auteur_type: str
    auteur_nom: str | None = None
    corps: str
    cree_le: datetime | None = None
    document_id: str | None = None


class DemandeOut(BaseModel):
    id: str
    numero: str = ""
    type: str
    sujet: str
    champ_concerne: str | None = None
    statut: str
    categorie: str | None = None
    sous_categorie: str | None = None
    motif_cloture: str | None = None
    cree_le: datetime | None = None
    membre_nom: str | None = None
    nb_messages: int = 0
    # Step tracking: when the staff took the request over, and when it closed.
    pris_en_charge_le: datetime | None = None
    clos_le: datetime | None = None


class DemandeDetail(DemandeOut):
    messages: list[MessageOut] = []


class DemandeDetailAdmin(DemandeDetail):
    """Admin view adds who handles the request (never shown to the member)."""

    pris_en_charge_par_email: str | None = None


# Controlled state machine of a ticket. Keys are current states, values the
# reachable states. Any other jump is rejected with a clear error.
STATUTS_LISIBLES = {
    "ouverte": "Ouverte",
    "en_cours": "En cours de traitement",
    "pieces_demandees": "Pièces demandées",
    "attente_membre": "En attente de votre réponse",
    "en_validation": "En validation",
    "resolue": "Résolue",
    "refusee": "Refusée",
}
_TRANSITIONS: dict[str, set[str]] = {
    "ouverte": {"en_cours", "pieces_demandees", "attente_membre", "resolue", "refusee"},
    "en_cours": {"pieces_demandees", "attente_membre", "en_validation", "resolue", "refusee"},
    "pieces_demandees": {"en_cours", "en_validation", "resolue", "refusee"},
    "attente_membre": {"en_cours", "resolue", "refusee"},
    "en_validation": {"en_cours", "resolue", "refusee"},
    # Reopening a closed ticket is an explicit admin decision.
    "resolue": {"en_cours"},
    "refusee": {"en_cours"},
}


def _numero(r: dict[str, object]) -> str:
    n = r.get("numero")
    return f"DEM-{int(n):06d}" if n is not None else ""


def _demande_row(r: dict[str, object]) -> DemandeOut:
    return DemandeOut(
        id=str(r["id"]),
        numero=_numero(r),
        type=str(r["type"]),
        sujet=str(r["sujet"]),
        champ_concerne=r.get("champ_concerne"),  # type: ignore[arg-type]
        statut=str(r["statut"]),
        categorie=r.get("categorie"),  # type: ignore[arg-type]
        sous_categorie=r.get("sous_categorie"),  # type: ignore[arg-type]
        motif_cloture=r.get("motif_cloture"),  # type: ignore[arg-type]
        cree_le=r.get("cree_le"),  # type: ignore[arg-type]
        membre_nom=r.get("membre_nom"),  # type: ignore[arg-type]
        nb_messages=int(r.get("nb_messages") or 0),
        pris_en_charge_le=r.get("pris_en_charge_le"),  # type: ignore[arg-type]
        clos_le=r.get("clos_le"),  # type: ignore[arg-type]
    )


def _prise_en_charge(demande_id: str, user: UserMe) -> bool:
    """Record the take-over (who, when) the first time a staff member acts on a
    request. Returns True when this call actually took the request over."""
    row = db.execute(
        "UPDATE demande SET pris_en_charge_par = %s, pris_en_charge_le = now() "
        "WHERE id = %s AND pris_en_charge_le IS NULL RETURNING id",
        (user.id, demande_id),
        role=user.role,
    )
    return bool(row)


def _system_message(demande_id: str, role: str | None, corps: str) -> None:
    """Timeline entry inside the thread for every workflow event."""
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'systeme', 'ADSUM', %s)",
        (demande_id, corps),
        role=role,
    )


def _notify_ticket(demande_id: str, role: str | None, titre: str, corps: str) -> None:
    """Multi-channel member notification about a ticket event (in-app + e-mail
    + Telegram through the standard dispatcher). Best-effort, never raises."""
    try:
        from . import channels

        owner = db.fetch_one("SELECT membre_id, numero FROM demande WHERE id = %s", (demande_id,), role=None)
        if not owner:
            return
        num = f"DEM-{int(owner['numero']):06d}" if owner.get("numero") is not None else ""
        channels.dispatch(
            str(owner["membre_id"]), role,
            channels.Message(titre=f"{titre} ({num})" if num else titre, corps_text=corps, type_notif="demande"),
        )
    except Exception:  # noqa: BLE001 - notification must never break the workflow
        pass


def _messages(demande_id: str, role: str) -> list[MessageOut]:
    rows = db.fetch_all(
        "SELECT id, auteur_type, auteur_nom, corps, cree_le, document_id FROM demande_message "
        "WHERE demande_id = %s ORDER BY cree_le ASC",
        (demande_id,),
        role=role,
    )
    return [
        MessageOut(
            id=str(m["id"]),
            auteur_type=str(m["auteur_type"]),
            auteur_nom=m["auteur_nom"],
            corps=str(m["corps"]),
            cree_le=m["cree_le"],
            document_id=str(m["document_id"]) if m.get("document_id") else None,
        )
        for m in rows
    ]


# --- Structured request catalog ---------------------------------------------
# Grounded in the real member-facing features (profile, identity documents,
# attachment to commissions/tribes, card and QR, activities and presence,
# notifications and language, account and security). Every category keeps an
# "autre" entry so no need is ever blocked. Messages are prewritten so members
# never have to compose from scratch; identity is attached server-side.
CATALOGUE: list[dict[str, object]] = [
    {"categorie": "profil", "libelle": "Mon profil et mon identité", "sous": [
        {"cle": "correction_nom", "libelle": "Corriger mon nom ou mes prénoms", "sujet": "Correction de mon nom ou de mes prénoms",
         "message": "Bonjour, je souhaite faire corriger mon nom ou mes prénoms sur mon profil. Voici la correction attendue : ", "piece": "recommandée"},
        {"cle": "correction_naissance", "libelle": "Corriger ma date de naissance", "sujet": "Correction de ma date de naissance",
         "message": "Bonjour, ma date de naissance est incorrecte sur mon profil. La date exacte est : ", "piece": "recommandée"},
        {"cle": "photo", "libelle": "Changer ma photo d'identité", "sujet": "Changement de ma photo d'identité",
         "message": "Bonjour, je souhaite mettre à jour ma photo d'identité. Merci de m'indiquer la marche à suivre ou de débloquer le remplacement.", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mon profil", "sujet": "Demande concernant mon profil",
         "message": "Bonjour, j'ai une demande concernant mon profil : ", "piece": "facultative"},
    ]},
    {"categorie": "coordonnees", "libelle": "Mes coordonnées", "sous": [
        {"cle": "telephone", "libelle": "Mettre à jour mon téléphone", "sujet": "Mise à jour de mon numéro de téléphone",
         "message": "Bonjour, mon numéro de téléphone a changé. Le nouveau numéro est : ", "piece": "facultative"},
        {"cle": "adresse", "libelle": "Mettre à jour ma ville ou mon adresse", "sujet": "Mise à jour de ma localisation",
         "message": "Bonjour, ma localisation a changé. Ma nouvelle ville (et mon quartier, si utile) est : ", "piece": "facultative"},
        {"cle": "email", "libelle": "Changer mon adresse e-mail", "sujet": "Changement de mon adresse e-mail",
         "message": "Bonjour, je souhaite changer l'adresse e-mail de mon compte. La nouvelle adresse est : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mes coordonnées", "sujet": "Demande concernant mes coordonnées",
         "message": "Bonjour, j'ai une demande concernant mes coordonnées : ", "piece": "facultative"},
    ]},
    {"categorie": "rattachement", "libelle": "Commission, tribu, intendance", "sous": [
        {"cle": "commission", "libelle": "Changer de commission", "sujet": "Demande de changement de commission",
         "message": "Bonjour, je souhaite changer de commission. Commission souhaitée et raison : ", "piece": "facultative"},
        {"cle": "tribu", "libelle": "Corriger ma tribu", "sujet": "Correction de ma tribu",
         "message": "Bonjour, ma tribu n'est pas correcte sur mon profil. Ma tribu est : ", "piece": "facultative"},
        {"cle": "intendance", "libelle": "Changer d'intendance ou de groupe", "sujet": "Changement d'intendance ou de groupe",
         "message": "Bonjour, je souhaite être rattaché(e) à une autre intendance ou un autre groupe : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande de rattachement", "sujet": "Demande concernant mon rattachement",
         "message": "Bonjour, j'ai une demande concernant mon rattachement : ", "piece": "facultative"},
    ]},
    {"categorie": "documents", "libelle": "Documents et pièces", "sous": [
        {"cle": "remplacer_piece", "libelle": "Remplacer ma pièce d'identité", "sujet": "Remplacement de ma pièce d'identité",
         "message": "Bonjour, je souhaite remplacer la pièce d'identité fournie lors de mon inscription (nouvelle pièce, renouvellement ou meilleure qualité).", "piece": "recommandée"},
        {"cle": "attestation", "libelle": "Question sur mon attestation signée", "sujet": "Question sur mon attestation d'engagement",
         "message": "Bonjour, j'ai une question concernant mon attestation d'engagement signée : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mes documents", "sujet": "Demande concernant mes documents",
         "message": "Bonjour, j'ai une demande concernant mes documents : ", "piece": "facultative"},
    ]},
    {"categorie": "carte", "libelle": "Ma carte et mon QR", "sous": [
        {"cle": "qr", "libelle": "Problème avec mon QR ou ma carte", "sujet": "Problème avec ma carte ou mon QR",
         "message": "Bonjour, je rencontre un problème avec ma carte de membre ou mon QR (affichage, scan refusé...). Voici ce qui se passe : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur ma carte", "sujet": "Demande concernant ma carte",
         "message": "Bonjour, j'ai une demande concernant ma carte de membre : ", "piece": "facultative"},
    ]},
    {"categorie": "activites", "libelle": "Activités et présences", "sous": [
        {"cle": "presence", "libelle": "Corriger une présence manquante", "sujet": "Correction d'une présence",
         "message": "Bonjour, j'ai participé à une activité mais ma présence n'apparaît pas dans mon historique. Activité et date : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur les activités", "sujet": "Demande concernant les activités",
         "message": "Bonjour, j'ai une demande concernant les activités : ", "piece": "facultative"},
    ]},
    {"categorie": "compte", "libelle": "Compte, sécurité et notifications", "sous": [
        {"cle": "connexion", "libelle": "Problème de connexion", "sujet": "Problème de connexion à mon compte",
         "message": "Bonjour, je rencontre un problème pour me connecter à mon compte. Voici ce qui se passe : ", "piece": "facultative"},
        {"cle": "notifications", "libelle": "Notifications ou langue", "sujet": "Demande sur mes notifications ou ma langue",
         "message": "Bonjour, j'ai une demande concernant mes notifications (canaux, fréquence) ou la langue de mon compte : ", "piece": "facultative"},
        {"cle": "autre", "libelle": "Autre demande sur mon compte", "sujet": "Demande concernant mon compte",
         "message": "Bonjour, j'ai une demande concernant mon compte : ", "piece": "facultative"},
    ]},
    {"categorie": "autre", "libelle": "Autre demande", "sous": [
        {"cle": "autre", "libelle": "Demande libre", "sujet": "Demande à l'administration",
         "message": "Bonjour, je souhaite contacter l'administration au sujet suivant : ", "piece": "facultative"},
    ]},
]


@router.get("/membres/me/demandes/catalogue")
def catalogue_demandes(ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> dict[str, object]:
    """Structured request catalog with prewritten messages, plus the readable
    status labels so the client never invents wording."""
    return {"categories": CATALOGUE, "statuts": STATUTS_LISIBLES}


# --- Member side -----------------------------------------------------------

@router.get("/membres/me/demandes", response_model=list[DemandeOut])
def my_demandes(ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> list[DemandeOut]:
    membre_id, role, _ = ctx
    rows = db.fetch_all(
        """
        SELECT d.id, d.numero, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le,
               (SELECT count(*) FROM demande_message m WHERE m.demande_id = d.id) AS nb_messages
        FROM demande d WHERE d.membre_id = %s ORDER BY d.maj_le DESC
        """,
        (membre_id,),
        role=role,
    )
    return [_demande_row(r) for r in rows]


@router.post("/membres/me/demandes", response_model=DemandeDetail, status_code=status.HTTP_201_CREATED)
def create_demande(payload: DemandeIn, ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> DemandeDetail:
    membre_id, role, _ = ctx
    nom_row = db.fetch_one(
        "SELECT trim(coalesce(prenoms,'')||' '||coalesce(nom,'')) AS nom FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    auteur = (nom_row["nom"] if nom_row and nom_row.get("nom") else "Membre") or "Membre"
    created = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, champ_concerne, categorie, sous_categorie) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, numero, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le",
        (membre_id, payload.type, payload.sujet, payload.champ_concerne, payload.categorie or payload.type, payload.sous_categorie),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="request not created")
    did = str(created["id"])
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) VALUES (%s, 'membre', %s, %s, %s)",
        (did, auteur, payload.message, payload.document_id),
        role=role,
    )
    out = _demande_row(created)
    _system_message(did, role, f"Demande {out.numero} ouverte. L'administration va l'examiner.")
    _notify_ticket(did, role, "Demande bien reçue",
                   "Votre demande a bien été enregistrée. Vous serez informé(e) de chaque étape de son traitement ici même.")
    return DemandeDetail(**out.model_dump(), messages=_messages(did, role))


@router.get("/membres/me/demandes/{demande_id}", response_model=DemandeDetail)
def my_demande(demande_id: str, ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> DemandeDetail:
    membre_id, role, _ = ctx
    row = db.fetch_one(
        "SELECT id, numero, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le FROM demande WHERE id = %s AND membre_id = %s",
        (demande_id, membre_id),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return DemandeDetail(**_demande_row(row).model_dump(), messages=_messages(demande_id, role))


@router.post("/membres/me/demandes/{demande_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def my_demande_reply(
    demande_id: str, payload: MessageIn, ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]
) -> MessageOut:
    membre_id, role, _ = ctx
    owns = db.fetch_one("SELECT id, statut FROM demande WHERE id = %s AND membre_id = %s", (demande_id, membre_id), role=role)
    if not owns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    nom_row = db.fetch_one(
        "SELECT trim(coalesce(prenoms,'')||' '||coalesce(nom,'')) AS nom FROM membre WHERE id = %s", (membre_id,), role=role
    )
    auteur = (nom_row["nom"] if nom_row and nom_row.get("nom") else "Membre") or "Membre"
    created = db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) VALUES (%s, 'membre', %s, %s, %s) "
        "RETURNING id, auteur_type, auteur_nom, corps, cree_le",
        (demande_id, auteur, payload.corps, payload.document_id),
        role=role,
    )
    db.execute("UPDATE demande SET maj_le = now() WHERE id = %s", (demande_id,), role=role)
    # A member answer to a documents/clarification request puts the ticket back
    # in the admin court, and the timeline says so.
    if str(owns.get("statut")) in ("pieces_demandees", "attente_membre"):
        db.execute("UPDATE demande SET statut = 'en_cours' WHERE id = %s", (demande_id,), role=role)
        _system_message(demande_id, role, "Réponse du membre reçue. La demande repasse en cours de traitement.")
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message not sent")
    return MessageOut(
        id=str(created["id"]), auteur_type="membre", auteur_nom=auteur, corps=payload.corps, cree_le=created["cree_le"]
    )


# --- Staff side ------------------------------------------------------------

@router.get("/admin/demandes", response_model=list[DemandeOut])
def admin_demandes(
    user: Annotated[UserMe, Depends(require_lecture)],
    statut: str | None = None,
    categorie: str | None = None,
    q: str | None = None,
    membre_id: str | None = None,
) -> list[DemandeOut]:
    where = ["1=1"]
    params: list[object] = []
    if statut:
        where.append("d.statut = %s")
        params.append(statut)
    if categorie:
        where.append("d.categorie = %s")
        params.append(categorie)
    if membre_id:
        # Per-member view: the full request history of one member's file.
        where.append("d.membre_id = %s")
        params.append(membre_id)
    if q:
        where.append("(d.sujet ILIKE %s OR trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    rows = db.fetch_all(
        f"""
        SELECT d.id, d.numero, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le,
               trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre_nom,
               (SELECT count(*) FROM demande_message dm WHERE dm.demande_id = d.id) AS nb_messages
        FROM demande d JOIN membre m ON m.id = d.membre_id
        WHERE {" AND ".join(where)}
        ORDER BY d.maj_le DESC
        """,
        tuple(params),
        role=user.role,
    )
    return [_demande_row(r) for r in rows]


@router.get("/admin/demandes/{demande_id}", response_model=DemandeDetailAdmin)
def admin_demande(demande_id: str, user: Annotated[UserMe, Depends(require_lecture)]) -> DemandeDetailAdmin:
    row = db.fetch_one(
        """
        SELECT d.id, d.numero, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le,
               trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre_nom,
               u.email AS agent_email
        FROM demande d JOIN membre m ON m.id = d.membre_id
        LEFT JOIN utilisateur u ON u.id = d.pris_en_charge_par
        WHERE d.id = %s
        """,
        (demande_id,),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return DemandeDetailAdmin(
        **_demande_row(row).model_dump(),
        messages=_messages(demande_id, user.role),
        pris_en_charge_par_email=row.get("agent_email"),
    )


@router.post("/admin/demandes/{demande_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def admin_reply(demande_id: str, payload: MessageIn, user: Annotated[UserMe, Depends(require_staff)]) -> MessageOut:
    created = db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) VALUES (%s, 'staff', 'Administration', %s, %s) "
        "RETURNING id, cree_le",
        (demande_id, payload.corps, payload.document_id),
        role=user.role,
    )
    db.execute("UPDATE demande SET maj_le = now(), statut = 'en_cours' WHERE id = %s AND statut = 'ouverte'", (demande_id,), role=user.role)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message not sent")
    # A first staff reply implicitly takes the request over: the timeline says so.
    if _prise_en_charge(demande_id, user):
        _system_message(demande_id, user.role, "Demande prise en charge par l'administration.")
    _notify_ticket(demande_id, user.role, "Réponse de l'administration",
                   "L'administration a répondu à votre demande. Ouvrez-la dans l'application pour lire la réponse et poursuivre l'échange.")
    return MessageOut(id=str(created["id"]), auteur_type="staff", auteur_nom="Administration", corps=payload.corps, cree_le=created["cree_le"])


@router.post("/admin/demandes/{demande_id}/prendre-en-charge", response_model=DemandeOut)
def admin_prendre_en_charge(demande_id: str, user: Annotated[UserMe, Depends(require_staff)]) -> DemandeOut:
    """Explicitly take a request over: records who and when, moves an open
    request to 'en cours', writes the timeline entry and informs the member."""
    from . import audit

    current = db.fetch_one("SELECT statut FROM demande WHERE id = %s", (demande_id,), role=user.role)
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    if str(current["statut"]) in ("resolue", "refusee"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request is closed")
    premiere = _prise_en_charge(demande_id, user)
    db.execute(
        "UPDATE demande SET statut = 'en_cours', maj_le = now() WHERE id = %s AND statut = 'ouverte'",
        (demande_id,),
        role=user.role,
    )
    if premiere:
        _system_message(demande_id, user.role, "Demande prise en charge par l'administration.")
        _notify_ticket(demande_id, user.role, "Demande prise en charge",
                       "Votre demande a été prise en charge par l'administration. Elle est maintenant en cours de traitement.")
        audit.log(user.id, user.role, "demande_prise_en_charge", "demande", demande_id, {})
    fresh = db.fetch_one(
        "SELECT id, numero, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le FROM demande WHERE id = %s",
        (demande_id,), role=user.role,
    )
    if not fresh:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _demande_row(fresh)


@router.post("/admin/demandes/{demande_id}/demander-piece", response_model=DemandeOut)
def admin_demander_piece(
    demande_id: str, payload: PieceRequeteIn, user: Annotated[UserMe, Depends(require_staff)]
) -> DemandeOut:
    """Ask the member for a supporting document INSIDE the same ticket: staff
    message describing the expected document, state moves to pieces_demandees,
    the member is notified on every channel, and the action is audited."""
    from . import audit

    row = db.fetch_one("SELECT id, statut FROM demande WHERE id = %s", (demande_id,), role=user.role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    if str(row["statut"]) in ("resolue", "refusee"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request is closed")
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'staff', 'Administration', %s)",
        (demande_id, f"Pièce demandée : {payload.description}"),
        role=user.role,
    )
    db.execute("UPDATE demande SET statut = 'pieces_demandees', maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
    if _prise_en_charge(demande_id, user):
        _system_message(demande_id, user.role, "Demande prise en charge par l'administration.")
    _system_message(demande_id, user.role, "Statut : pièces demandées. La demande attend votre réponse.")
    _notify_ticket(demande_id, user.role, "Pièce justificative demandée",
                   f"L'administration a besoin d'une pièce pour traiter votre demande : {payload.description}. "
                   "Ouvrez la demande dans l'application, répondez-y et joignez le document demandé.")
    audit.log(user.id, user.role, "demande_piece_requise", "demande", demande_id, {"description": payload.description})
    fresh = db.fetch_one(
        "SELECT id, numero, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le FROM demande WHERE id = %s",
        (demande_id,), role=user.role,
    )
    return _demande_row(fresh or row)


@router.patch("/admin/demandes/{demande_id}", response_model=DemandeOut)
def admin_update(demande_id: str, payload: DemandePatch, user: Annotated[UserMe, Depends(require_staff)]) -> DemandeOut:
    from . import audit

    if payload.statut:
        if payload.statut not in STATUTS_LISIBLES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown status")
        current = db.fetch_one("SELECT statut FROM demande WHERE id = %s", (demande_id,), role=user.role)
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
        allowed = _TRANSITIONS.get(str(current["statut"]), set())
        if payload.statut != str(current["statut"]) and payload.statut not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"transition_invalide": True, "de": current["statut"], "vers": payload.statut},
            )
        closing = payload.statut in ("resolue", "refusee")
        # Any staff-driven transition on a not-yet-handled request takes it over.
        if _prise_en_charge(demande_id, user):
            _system_message(demande_id, user.role, "Demande prise en charge par l'administration.")
        db.execute(
            "UPDATE demande SET statut = %s, maj_le = now(), "
            "motif_cloture = CASE WHEN %s THEN %s ELSE motif_cloture END, "
            "clos_le = CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE id = %s",
            (payload.statut, closing, payload.motif, closing, demande_id),
            role=user.role,
        )
        libelle = STATUTS_LISIBLES.get(payload.statut, payload.statut)
        _system_message(demande_id, user.role, f"Statut : {libelle}." + (f" Motif : {payload.motif}" if closing and payload.motif else ""))
        if closing:
            _notify_ticket(demande_id, user.role,
                           "Demande résolue" if payload.statut == "resolue" else "Demande refusée",
                           ("Votre demande a été traitée et résolue. Merci de votre confiance."
                            if payload.statut == "resolue"
                            else f"Votre demande a été refusée. Motif : {payload.motif or 'non précisé'}."))
        elif payload.statut == "en_cours" and str(current["statut"]) in ("resolue", "refusee"):
            _notify_ticket(demande_id, user.role, "Demande rouverte",
                           "Votre demande a été rouverte par l'administration et repasse en cours de traitement.")
        audit.log(user.id, user.role, "demande_statut", "demande", demande_id,
                  {"de": str(current["statut"]), "vers": payload.statut, "motif": payload.motif})
    if payload.champs_deverrouilles is not None:
        owner = db.fetch_one("SELECT membre_id FROM demande WHERE id = %s", (demande_id,), role=user.role)
        if owner:
            db.execute(
                "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
                (payload.champs_deverrouilles, str(owner["membre_id"])),
                role=user.role,
            )
        _notify_member(demande_id, user.role, "Modification autorisée", "L'administration a débloqué la modification demandée.")
    row = db.fetch_one(
        "SELECT id, numero, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le FROM demande WHERE id = %s", (demande_id,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _demande_row(row)


# --- Final validation of member data modifications -------------------------

# Defense-in-depth whitelist: only these member columns can be committed from a
# pending proposal, even though update_mon_profil already filtered the input.
_COMMITTABLE_FIELDS = frozenset(
    {
        "prenoms", "nom", "telephone", "date_naissance", "genre", "pays", "ville",
        "commission_id", "intendance_id", "tribu_id", "groupe", "profession",
        "niveau_etudes", "situation_matrimoniale", "type_mariage",
        "baptise", "confirme", "premiere_communion",
    }
)


class ModifDecision(BaseModel):
    decision: str  # 'valider' | 'rejeter'


@router.get("/admin/demandes/{demande_id}/modifications")
def admin_demande_modifications(demande_id: str, user: Annotated[UserMe, Depends(require_lecture)]) -> list[dict[str, object]]:
    """Pending and past member modifications attached to this request, as a diff."""
    rows = db.fetch_all(
        "SELECT id, valeurs, valeurs_avant, statut, propose_le, decide_le "
        "FROM modification_membre WHERE demande_id = %s ORDER BY propose_le DESC",
        (demande_id,),
        role=user.role,
    )
    out: list[dict[str, object]] = []
    for r in rows:
        valeurs = r["valeurs"] or {}
        avant = r["valeurs_avant"] or {}
        diff = [{"champ": k, "avant": avant.get(k), "apres": v} for k, v in valeurs.items()]
        out.append(
            {
                "id": str(r["id"]),
                "statut": r["statut"],
                "propose_le": r["propose_le"].isoformat() if r["propose_le"] else None,
                "decide_le": r["decide_le"].isoformat() if r["decide_le"] else None,
                "diff": diff,
            }
        )
    return out


@router.post("/admin/demandes/{demande_id}/modifications/decision")
def admin_decide_modification(
    demande_id: str, payload: ModifDecision, user: Annotated[UserMe, Depends(require_staff)]
) -> dict[str, object]:
    """Give the final validation on a pending member modification.

    On 'valider' the proposed values are committed to the member record, the
    unlocked fields are re-locked and the request is resolved. On 'rejeter'
    nothing is committed. The member is notified either way.
    """
    if payload.decision not in ("valider", "rejeter"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="decision must be valider or rejeter")
    mod = db.fetch_one(
        "SELECT id, membre_id, valeurs FROM modification_membre "
        "WHERE demande_id = %s AND statut = 'en_attente' ORDER BY propose_le DESC LIMIT 1",
        (demande_id,),
        role=user.role,
    )
    if not mod:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no pending modification")
    membre_id = str(mod["membre_id"])
    if payload.decision == "valider":
        applied = {k: v for k, v in (mod["valeurs"] or {}).items() if k in _COMMITTABLE_FIELDS}
        if applied:
            sets = ", ".join(f"{k} = %s" for k in applied)
            db.execute(f"UPDATE membre SET {sets} WHERE id = %s", (*applied.values(), membre_id), role=user.role)
        db.execute(
            "UPDATE modification_membre SET statut = 'validee', decide_le = now(), decide_par = %s WHERE id = %s",
            (user.id, str(mod["id"])),
            role=user.role,
        )
        db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=user.role)
        db.execute("UPDATE demande SET statut = 'resolue', maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
        _notify_member(demande_id, user.role, "Modification validée", "Votre modification a été validée et enregistrée.")
        return {"ok": True, "statut": "validee", "champs": list(applied)}
    db.execute(
        "UPDATE modification_membre SET statut = 'rejetee', decide_le = now(), decide_par = %s WHERE id = %s",
        (user.id, str(mod["id"])),
        role=user.role,
    )
    db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=user.role)
    db.execute("UPDATE demande SET statut = 'refusee', maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
    _notify_member(demande_id, user.role, "Modification refusée", "Votre modification n'a pas été validée. Contactez l'administration.")
    return {"ok": True, "statut": "rejetee"}


def _notify_member(demande_id: str, role: str, titre: str, corps: str) -> None:
    row = db.fetch_one("SELECT membre_id FROM demande WHERE id = %s", (demande_id,), role=role)
    if not row:
        return
    db.execute(
        "INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le) VALUES (%s, 'demande', %s, %s, false, now())",
        (str(row["membre_id"]), titre, corps),
        role=role,
    )
