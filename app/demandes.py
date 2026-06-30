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


class MessageIn(BaseModel):
    corps: str


class DemandePatch(BaseModel):
    statut: str | None = None
    champs_deverrouilles: list[str] | None = None


class MessageOut(BaseModel):
    id: str
    auteur_type: str
    auteur_nom: str | None = None
    corps: str
    cree_le: datetime | None = None


class DemandeOut(BaseModel):
    id: str
    type: str
    sujet: str
    champ_concerne: str | None = None
    statut: str
    cree_le: datetime | None = None
    membre_nom: str | None = None
    nb_messages: int = 0


class DemandeDetail(DemandeOut):
    messages: list[MessageOut] = []


def _demande_row(r: dict[str, object]) -> DemandeOut:
    return DemandeOut(
        id=str(r["id"]),
        type=str(r["type"]),
        sujet=str(r["sujet"]),
        champ_concerne=r.get("champ_concerne"),  # type: ignore[arg-type]
        statut=str(r["statut"]),
        cree_le=r.get("cree_le"),  # type: ignore[arg-type]
        membre_nom=r.get("membre_nom"),  # type: ignore[arg-type]
        nb_messages=int(r.get("nb_messages") or 0),
    )


def _messages(demande_id: str, role: str) -> list[MessageOut]:
    rows = db.fetch_all(
        "SELECT id, auteur_type, auteur_nom, corps, cree_le FROM demande_message "
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
        )
        for m in rows
    ]


# --- Member side -----------------------------------------------------------

@router.get("/membres/me/demandes", response_model=list[DemandeOut])
def my_demandes(ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> list[DemandeOut]:
    membre_id, role, _ = ctx
    rows = db.fetch_all(
        """
        SELECT d.id, d.type, d.sujet, d.champ_concerne, d.statut, d.cree_le,
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
        "INSERT INTO demande (membre_id, type, sujet, champ_concerne) VALUES (%s, %s, %s, %s) RETURNING id, type, sujet, champ_concerne, statut, cree_le",
        (membre_id, payload.type, payload.sujet, payload.champ_concerne),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="request not created")
    did = str(created["id"])
    db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'membre', %s, %s)",
        (did, auteur, payload.message),
        role=role,
    )
    out = _demande_row(created)
    return DemandeDetail(**out.model_dump(), messages=_messages(did, role))


@router.get("/membres/me/demandes/{demande_id}", response_model=DemandeDetail)
def my_demande(demande_id: str, ctx: Annotated[tuple[str, str, str], Depends(_require_membre)]) -> DemandeDetail:
    membre_id, role, _ = ctx
    row = db.fetch_one(
        "SELECT id, type, sujet, champ_concerne, statut, cree_le FROM demande WHERE id = %s AND membre_id = %s",
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
    owns = db.fetch_one("SELECT id FROM demande WHERE id = %s AND membre_id = %s", (demande_id, membre_id), role=role)
    if not owns:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    nom_row = db.fetch_one(
        "SELECT trim(coalesce(prenoms,'')||' '||coalesce(nom,'')) AS nom FROM membre WHERE id = %s", (membre_id,), role=role
    )
    auteur = (nom_row["nom"] if nom_row and nom_row.get("nom") else "Membre") or "Membre"
    created = db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'membre', %s, %s) "
        "RETURNING id, auteur_type, auteur_nom, corps, cree_le",
        (demande_id, auteur, payload.corps),
        role=role,
    )
    db.execute("UPDATE demande SET maj_le = now() WHERE id = %s", (demande_id,), role=role)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message not sent")
    return MessageOut(
        id=str(created["id"]), auteur_type="membre", auteur_nom=auteur, corps=payload.corps, cree_le=created["cree_le"]
    )


# --- Staff side ------------------------------------------------------------

@router.get("/admin/demandes", response_model=list[DemandeOut])
def admin_demandes(user: Annotated[UserMe, Depends(require_lecture)]) -> list[DemandeOut]:
    rows = db.fetch_all(
        """
        SELECT d.id, d.type, d.sujet, d.champ_concerne, d.statut, d.cree_le,
               trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre_nom,
               (SELECT count(*) FROM demande_message dm WHERE dm.demande_id = d.id) AS nb_messages
        FROM demande d JOIN membre m ON m.id = d.membre_id
        ORDER BY d.maj_le DESC
        """,
        (),
        role=user.role,
    )
    return [_demande_row(r) for r in rows]


@router.get("/admin/demandes/{demande_id}", response_model=DemandeDetail)
def admin_demande(demande_id: str, user: Annotated[UserMe, Depends(require_lecture)]) -> DemandeDetail:
    row = db.fetch_one(
        """
        SELECT d.id, d.type, d.sujet, d.champ_concerne, d.statut, d.cree_le,
               trim(coalesce(m.prenoms,'')||' '||coalesce(m.nom,'')) AS membre_nom
        FROM demande d JOIN membre m ON m.id = d.membre_id WHERE d.id = %s
        """,
        (demande_id,),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return DemandeDetail(**_demande_row(row).model_dump(), messages=_messages(demande_id, user.role))


@router.post("/admin/demandes/{demande_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def admin_reply(demande_id: str, payload: MessageIn, user: Annotated[UserMe, Depends(require_staff)]) -> MessageOut:
    created = db.execute(
        "INSERT INTO demande_message (demande_id, auteur_type, auteur_nom, corps) VALUES (%s, 'staff', 'Administration', %s) "
        "RETURNING id, cree_le",
        (demande_id, payload.corps),
        role=user.role,
    )
    db.execute("UPDATE demande SET maj_le = now(), statut = 'en_cours' WHERE id = %s AND statut = 'ouverte'", (demande_id,), role=user.role)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message not sent")
    _notify_member(demande_id, user.role, "Réponse de l'administration", "L'administration a répondu à votre demande.")
    return MessageOut(id=str(created["id"]), auteur_type="staff", auteur_nom="Administration", corps=payload.corps, cree_le=created["cree_le"])


@router.patch("/admin/demandes/{demande_id}", response_model=DemandeOut)
def admin_update(demande_id: str, payload: DemandePatch, user: Annotated[UserMe, Depends(require_staff)]) -> DemandeOut:
    if payload.statut:
        db.execute("UPDATE demande SET statut = %s, maj_le = now() WHERE id = %s", (payload.statut, demande_id), role=user.role)
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
        "SELECT id, type, sujet, champ_concerne, statut, cree_le FROM demande WHERE id = %s", (demande_id,), role=user.role
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
