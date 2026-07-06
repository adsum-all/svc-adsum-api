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
from pydantic import BaseModel, Field

from . import db, identifiants
from .auth import current_user
from .demandes_catalogue import _TRANSITIONS, CATALOGUE, STATUTS_LISIBLES
from .fields import ShortStr, TextStr, TitleStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["demandes"])



def _require_membre(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role, user.email


class DemandeIn(BaseModel):
    type: ShortStr = "question"
    sujet: TitleStr
    champ_concerne: ShortStr | None = None
    message: TextStr
    categorie: ShortStr | None = None
    sous_categorie: ShortStr | None = None
    # Optional supporting document: never required to submit (absolute rule).
    document_id: ShortStr | None = None


class MessageIn(BaseModel):
    corps: TextStr
    # Optional supporting document uploaded through the standard document flow,
    # attached to this very ticket message (rule: files live inside the thread).
    document_id: ShortStr | None = None


class DemandePatch(BaseModel):
    statut: ShortStr | None = None
    champs_deverrouilles: list[ShortStr] | None = None
    motif: TextStr | None = None
    # Response window (days) granted with an unlock; falls back to the central
    # admin parameter deblocage_delai_jours when omitted.
    delai_jours: int | None = Field(default=None, ge=1, le=90)


class PieceRequeteIn(BaseModel):
    description: TextStr


class MessageOut(BaseModel):
    id: str
    auteur_type: str
    auteur_nom: str | None = None
    corps: str
    cree_le: datetime | None = None
    document_id: str | None = None
    # Read receipts: both parties can see when the other side read a message.
    lu_par_membre_le: datetime | None = None
    lu_par_staff_le: datetime | None = None


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
    # Deadline for the member's action after an unlock (auto-close when over).
    echeance_reponse: datetime | None = None


class DemandeDetail(DemandeOut):
    messages: list[MessageOut] = []


class DemandeDetailAdmin(DemandeDetail):
    """Admin view adds who handles the request (never shown to the member)."""

    pris_en_charge_par_email: str | None = None




def _numero(r: dict[str, object]) -> str:
    """Human reference of a request. Prefers the durable DEM-YYYY-NNNNNN
    reference; falls back to the legacy DEM-NNNNNN for rows not yet backfilled."""
    ref = r.get("reference")
    if ref:
        return str(ref)
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
        echeance_reponse=r.get("echeance_reponse"),  # type: ignore[arg-type]
    )


def _sync_attestation_liee(demande_id: str, statut: str, motif: str | None, user: UserMe) -> None:
    """Keep the linked attestation task (and the member profile flag) in step
    with the ticket. The administration can act from the Demandes screen or
    from the Attestations screen: both must tell the member the same truth."""
    att = db.fetch_one(
        "SELECT id, membre_id, document_id FROM attestation_manuelle WHERE demande_id = %s",
        (demande_id,),
        role=user.role,
    )
    if not att:
        return
    if statut == "resolue":
        nouveau = "accepted"
    elif statut == "refusee":
        nouveau = "rejected"
    elif statut == "en_cours":
        # Reopened ticket: the attestation goes back to review (scan present)
        # or to awaiting the member's upload.
        nouveau = "under_review" if att.get("document_id") else "awaiting"
    else:
        return
    db.execute(
        "UPDATE attestation_manuelle SET statut = %s, "
        "valide_le = CASE WHEN %s IN ('accepted', 'rejected') THEN now() ELSE NULL END, "
        "valide_par = CASE WHEN %s IN ('accepted', 'rejected') THEN %s::uuid ELSE NULL END, "
        "motif_rejet = CASE WHEN %s = 'rejected' THEN %s ELSE NULL END "
        "WHERE id = %s",
        (nouveau, nouveau, nouveau, user.id, nouveau, motif, str(att["id"])),
        role=user.role,
    )
    db.execute(
        "UPDATE membre SET attestation_statut = %s WHERE id = %s",
        (nouveau, str(att["membre_id"])),
        role=user.role,
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


def _delai_deblocage_defaut(role: str | None) -> int:
    """Central response window (days) after an unlock. The business source
    mentions both 14 and 30 days; until that arbitration lands the value lives
    in the admin parameter deblocage_delai_jours (seeded at 30, the least
    destructive default) and is never hardcoded."""
    row = db.fetch_one(
        "SELECT (valeur #>> '{}')::int AS j FROM parametre WHERE cle = 'deblocage_delai_jours'",
        (),
        role=role,
    )
    try:
        return max(1, min(90, int((row or {}).get("j") or 30)))
    except (TypeError, ValueError):
        return 30


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

        owner = db.fetch_one("SELECT membre_id, numero, reference FROM demande WHERE id = %s", (demande_id,), role=None)
        if not owner:
            return
        num = _numero(owner)
        channels.dispatch(
            str(owner["membre_id"]), role,
            channels.Message(titre=f"{titre} ({num})" if num else titre, corps_text=corps, type_notif="demande"),
        )
    except Exception:  # noqa: BLE001 - notification must never break the workflow
        pass


def _messages(demande_id: str, role: str) -> list[MessageOut]:
    rows = db.fetch_all(
        "SELECT id, auteur_type, auteur_nom, corps, cree_le, document_id, lu_par_membre_le, lu_par_staff_le FROM demande_message "
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
            lu_par_membre_le=m.get("lu_par_membre_le"),
            lu_par_staff_le=m.get("lu_par_staff_le"),
        )
        for m in rows
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
        SELECT d.id, d.numero, d.reference, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le, d.echeance_reponse,
               (SELECT count(*) FROM demande_message m WHERE m.demande_id = d.id) AS nb_messages
        FROM demande d WHERE d.membre_id = %s ORDER BY d.maj_le DESC
        """,
        (membre_id,),
        role=role,
    )
    return [_demande_row(r) for r in rows]


def _validated_document_id(document_id: str | None, membre_id: str, role: str) -> str | None:
    """Ensure an attached document belongs to the current member, else reject.

    Without this check a member could attach any document_id, including another
    member's, to their own ticket: the staff agent handling it would then see and
    could open a third party's identity file from this thread. Ownership is not
    verified elsewhere on this write path, so it is enforced here.
    """
    if not document_id:
        return None
    owns = db.fetch_one(
        "SELECT 1 FROM document WHERE id = %s AND membre_id = %s",
        (document_id, membre_id),
        role=role,
    )
    if not owns:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown document")
    return document_id


@router.post("/membres/me/demandes", response_model=DemandeDetail, status_code=status.HTTP_201_CREATED)
def create_demande(payload: DemandeIn, ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> DemandeDetail:
    membre_id, role, _ = ctx
    nom_row = db.fetch_one(
        "SELECT trim(coalesce(prenoms,'')||' '||coalesce(nom,'')) AS nom FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    auteur = (nom_row["nom"] if nom_row and nom_row.get("nom") else "Membre") or "Membre"
    reference = identifiants.next_reference(role)
    created = db.execute(
        "INSERT INTO demande (membre_id, type, sujet, champ_concerne, categorie, sous_categorie, reference) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, numero, reference, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le, echeance_reponse",
        (membre_id, payload.type, payload.sujet, payload.champ_concerne, payload.categorie or payload.type, payload.sous_categorie, reference),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="request not created")
    did = str(created["id"])
    doc_id = _validated_document_id(payload.document_id, membre_id, role)
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) VALUES (%s, 'membre', %s, %s, %s)",
        (did, auteur, payload.message, doc_id),
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
        "SELECT id, numero, reference, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le, echeance_reponse FROM demande WHERE id = %s AND membre_id = %s",
        (demande_id, membre_id),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    # Read receipt: opening the conversation is the read event for everything
    # the administration (or the system) wrote. Idempotent by construction.
    db.execute(
        "UPDATE demande_message SET lu_par_membre_le = now() "
        "WHERE demande_id = %s AND auteur_type IN ('staff', 'systeme') AND lu_par_membre_le IS NULL",
        (demande_id,),
        role=role,
    )
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
    doc_id = _validated_document_id(payload.document_id, membre_id, role)
    created = db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps, document_id) VALUES (%s, 'membre', %s, %s, %s) "
        "RETURNING id, auteur_type, auteur_nom, corps, cree_le",
        (demande_id, auteur, payload.corps, doc_id),
        role=role,
    )
    db.execute("UPDATE demande SET maj_le = now() WHERE id = %s", (demande_id,), role=role)
    # A member answer to a documents/clarification request puts the ticket back
    # in the admin court, and the timeline says so.
    if str(owns.get("statut")) in ("pieces_demandees", "attente_membre"):
        db.execute("UPDATE demande SET statut = 'en_cours', echeance_reponse = NULL WHERE id = %s", (demande_id,), role=role)
        _system_message(demande_id, role, "Réponse du membre reçue. La demande repasse en cours de traitement.")
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message not sent")
    return MessageOut(
        id=str(created["id"]), auteur_type="membre", auteur_nom=auteur, corps=payload.corps, cree_le=created["cree_le"]
    )


# --- Staff side ------------------------------------------------------------

@router.get("/admin/demandes", response_model=list[DemandeOut])
def admin_demandes(
    user: Annotated[UserMe, Depends(require_permission("demandes.superviser"))],
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
        SELECT d.id, d.numero, d.reference, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le, d.echeance_reponse,
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
def admin_demande(demande_id: str, user: Annotated[UserMe, Depends(require_permission("demandes.superviser"))]) -> DemandeDetailAdmin:
    row = db.fetch_one(
        """
        SELECT d.id, d.numero, d.reference, d.type, d.sujet, d.champ_concerne, d.statut, d.categorie, d.sous_categorie, d.motif_cloture, d.cree_le, d.pris_en_charge_le, d.clos_le, d.echeance_reponse,
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
    db.execute(
        "UPDATE demande_message SET lu_par_staff_le = now() "
        "WHERE demande_id = %s AND auteur_type = 'membre' AND lu_par_staff_le IS NULL",
        (demande_id,),
        role=user.role,
    )
    return DemandeDetailAdmin(
        **_demande_row(row).model_dump(),
        messages=_messages(demande_id, user.role),
        pris_en_charge_par_email=row.get("agent_email"),
    )


@router.post("/admin/demandes/{demande_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def admin_reply(demande_id: str, payload: MessageIn, user: Annotated[UserMe, Depends(require_permission("demandes.gerer"))]) -> MessageOut:
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
def admin_prendre_en_charge(demande_id: str, user: Annotated[UserMe, Depends(require_permission("demandes.gerer"))]) -> DemandeOut:
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
        "SELECT id, numero, reference, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le, echeance_reponse FROM demande WHERE id = %s",
        (demande_id,), role=user.role,
    )
    if not fresh:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _demande_row(fresh)


@router.post("/admin/demandes/{demande_id}/demander-piece", response_model=DemandeOut)
def admin_demander_piece(
    demande_id: str, payload: PieceRequeteIn, user: Annotated[UserMe, Depends(require_permission("demandes.gerer"))]
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
        "SELECT id, numero, reference, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le, echeance_reponse FROM demande WHERE id = %s",
        (demande_id,), role=user.role,
    )
    return _demande_row(fresh or row)


@router.patch("/admin/demandes/{demande_id}", response_model=DemandeOut)
def admin_update(demande_id: str, payload: DemandePatch, user: Annotated[UserMe, Depends(require_permission("demandes.gerer"))]) -> DemandeOut:
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
        # A ticket linked to an attestation task carries the same truth: closing
        # or reopening the ticket updates the attestation and the member profile.
        _sync_attestation_liee(demande_id, payload.statut, payload.motif, user)
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
        from .deblocage import ELEMENTS, libelles

        inconnus = [c for c in payload.champs_deverrouilles if c not in ELEMENTS]
        if inconnus:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"éléments inconnus du catalogue de déblocage : {', '.join(inconnus)}")
        owner = db.fetch_one("SELECT membre_id, statut FROM demande WHERE id = %s", (demande_id,), role=user.role)
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
        if str(owner["statut"]) in ("resolue", "refusee"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request is closed")
        db.execute(
            "UPDATE membre SET champs_deverrouilles = %s WHERE id = %s",
            (payload.champs_deverrouilles, str(owner["membre_id"])),
            role=user.role,
        )
        if payload.champs_deverrouilles:
            # A new cycle opens: drop any proposal left pending from a previous
            # cycle so the single-pending-per-request invariant (and its unique
            # index) always holds for the fresh submission.
            db.execute(
                "UPDATE modification_membre SET statut = 'rejetee', decide_le = now() "
                "WHERE demande_id = %s AND statut = 'en_attente'",
                (demande_id,),
                role=user.role,
            )
            # Grant a response window (per-request override, else the central
            # admin parameter; the 14-vs-30-day business arbitration is pending,
            # so the value is configurable, never hardcoded).
            delai = payload.delai_jours or _delai_deblocage_defaut(user.role)
            echeance = db.fetch_one(
                "UPDATE demande SET statut = 'attente_membre', echeance_reponse = now() + make_interval(days => %s), maj_le = now() "
                "WHERE id = %s RETURNING to_char(echeance_reponse, 'DD/MM/YYYY') AS d",
                (delai, demande_id),
                role=user.role,
            )
            date_limite = (echeance or {}).get("d") or ""
            noms = ", ".join(libelles(payload.champs_deverrouilles))
            if _prise_en_charge(demande_id, user):
                _system_message(demande_id, user.role, "Demande prise en charge par l'administration.")
            _system_message(
                demande_id, user.role,
                f"Éléments débloqués pour correction : {noms}. Faites la mise à jour demandée puis répondez dans cette "
                f"demande avant le {date_limite}. Sans retour de votre part à cette date, la demande sera clôturée "
                "automatiquement sans suite.",
            )
            _notify_ticket(demande_id, user.role, "Correction attendue",
                           f"L'administration a débloqué : {noms}. Faites la mise à jour puis répondez dans la demande "
                           f"avant le {date_limite}.")
            audit.log(user.id, user.role, "deblocage_elements", "demande", demande_id,
                      {"elements": payload.champs_deverrouilles, "delai_jours": delai})
        else:
            # Explicit re-lock (empty list): the window closes with it.
            db.execute("UPDATE demande SET echeance_reponse = NULL, maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
            _system_message(demande_id, user.role, "Les éléments débloqués ont été reverrouillés par l'administration.")
            audit.log(user.id, user.role, "reverrouillage_elements", "demande", demande_id, {})
    row = db.fetch_one(
        "SELECT id, numero, reference, type, sujet, champ_concerne, statut, categorie, sous_categorie, motif_cloture, cree_le, pris_en_charge_le, clos_le, echeance_reponse FROM demande WHERE id = %s", (demande_id,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _demande_row(row)


@router.get("/admin/deblocage/elements")
def catalogue_deblocage(user: Annotated[UserMe, Depends(require_permission("deblocage.superviser"))]) -> list[dict[str, str]]:
    """Extensible catalog of unlockable elements (fields, photo, documents)."""
    from .deblocage import ELEMENTS

    return [
        {"cle": cle, "libelle": meta["libelle"], "type": meta["type"], "sensibilite": meta["sensibilite"]}
        for cle, meta in ELEMENTS.items()
    ]
