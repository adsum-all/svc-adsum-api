"""Moderator channel: voice-note instruction requests for the collaboration team.

The founder or moderator drops requests here, most often as voice notes, sometimes
several at once. Each message carries a written note and/or an audio note plus
attachments, a two-level transcription (verbatim raw text and a faithful
professional redaction, both editable) and a reply thread. A message can be turned
into a real card. Reads are guarded by ``canal.voir``, writes by ``canal.traiter``,
and every read/write is additionally isolated to the workspaces the caller belongs
to (see ``_espaces_accessibles``/``_garde_message``). Audio and attachments reuse the
shared private-bucket storage; transcription goes through the ``ai_providers`` layer.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import attachments_core as att
from . import audit, db, storage
from .collaboration_cartes import _espace_of_tableau
from .collaboration_espaces import require_espace_role
from .fields import LineStr, ShortStr, TextStr
from .permissions_rbac import require_permission
from .sanitize import sanitize_html
from .schemas import UserMe

MEMBRES_ACTIFS = ("proprietaire", "admin", "membre")

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-canal"])

CATEGORIES = ("projet", "activite", "organisation", "communication", "urgence", "autre")
PRIORITES = ("urgente", "haute", "normale", "basse")
STATUTS = ("nouveau", "en_cours", "traite", "archive")
_AUDIO_MAX = 24 * 1024 * 1024
_MSG_COLS = (
    "id, auteur_id, espace_id, assigne_id, carte_id, parent_id, categorie, priorite, statut, sujet, note, "
    "transcription_brute, transcription_redigee, transcription_statut, transcription_erreur, "
    "audio_conserver, cree_le, maj_le, "
    "matricule, etape, prise_en_charge_par, prise_en_charge_le, assigne_espace_id, "
    "restitution_rapport, restitution_resume, cloture_le, cloture_par"
)


class CanalPieceOut(BaseModel):
    id: str
    nom: str
    taille: int
    type: str
    cree_le: str | None = None
    data_url: str | None = None
    duree_s: int | None = None
    est_audio: bool = False


class CanalReponseOut(BaseModel):
    id: str
    auteur_id: str
    corps: str
    cree_le: str | None = None


class CanalNoteOut(BaseModel):
    """One voice note inside a channel, with its own transcription and redaction."""
    id: str
    ordre: int
    audio: CanalPieceOut | None = None
    transcription_brute: str
    transcription_redigee: str
    transcription_statut: str
    transcription_erreur: str | None = None
    audio_conserver: bool = False
    audio_expire_jours: int | None = None
    cree_le: str | None = None


class CanalLienOut(BaseModel):
    """A lightweight reference to a linked channel (for the continuity tree)."""
    id: str
    sujet: str
    priorite: str
    statut: str
    cree_le: str | None = None


class CanalMessageOut(BaseModel):
    id: str
    auteur_id: str
    espace_id: str | None = None
    espace_nom: str | None = None
    assigne_id: str | None = None
    carte_id: str | None = None
    parent_id: str | None = None
    categorie: str
    priorite: str
    statut: str
    sujet: str
    note: str
    # First note mirrored at the top level for backward compatibility with clients that
    # still read a single transcription; new clients use ``notes``.
    transcription_brute: str
    transcription_redigee: str
    transcription_statut: str
    transcription_erreur: str | None = None
    audio: CanalPieceOut | None = None
    audio_conserver: bool = False
    audio_expire_jours: int | None = None
    notes: list[CanalNoteOut]
    parent: CanalLienOut | None = None
    enfants: list[CanalLienOut]
    pieces: list[CanalPieceOut]
    reponses: list[CanalReponseOut]
    cree_le: str | None = None
    # Instruction workflow (see collaboration_canal_workflow).
    matricule: str | None = None
    etape: str = "recue"
    prise_en_charge_par: str | None = None
    prise_en_charge_nom: str | None = None
    prise_en_charge_le: str | None = None
    assigne_espace_id: str | None = None
    assigne_espace_nom: str | None = None
    assignes: list[str] = []
    restitution_rapport: str | None = None
    restitution_resume: str | None = None
    cloture_le: str | None = None
    cloture_par: str | None = None


class CanalMessageIn(BaseModel):
    sujet: LineStr = ""
    note: TextStr = ""
    categorie: ShortStr = "autre"
    priorite: ShortStr = "normale"
    espace_id: ShortStr | None = None


class CanalPatch(BaseModel):
    # Channel-level fields only. Transcriptions and the audio-retention flag live on
    # each note now (PATCH /canal/notes/{note_id}), so they are not patchable here.
    sujet: LineStr | None = None
    note: TextStr | None = None
    categorie: ShortStr | None = None
    priorite: ShortStr | None = None
    statut: ShortStr | None = None
    assigne_id: ShortStr | None = None
    espace_id: ShortStr | None = None
    # Link to a parent channel (continuity tree); empty string clears the link.
    parent_id: ShortStr | None = None


class PieceIn(BaseModel):
    nom: str
    type: str = ""
    data_url: str
    duree_s: int | None = None


class ReponseIn(BaseModel):
    corps: TextStr


class CreerCarteIn(BaseModel):
    tableau_id: ShortStr
    colonne_id: ShortStr


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _piece_out(row: dict[str, Any], served: dict[str, str]) -> CanalPieceOut:
    return CanalPieceOut(
        id=str(row["id"]),
        nom=row["nom"],
        taille=int(row.get("taille") or 0),
        type=row.get("type") or "",
        cree_le=_iso(row.get("cree_le")),
        data_url=served.get(str(row["id"]), ""),
        duree_s=row.get("duree_s"),
        est_audio=bool(row.get("est_audio")),
    )


def _assemble(
    rows: list[dict[str, Any]], role: str, espaces_ok: set[str] | None = None
) -> list[CanalMessageOut]:
    """Assemble messages with their pieces and replies in batched queries (single
    connection style), signing every attachment URL in one bulk call.

    ``espaces_ok`` (None for admins) restricts the parent/child continuity blocks to
    those workspaces, so a linked instruction in another workspace never leaks its
    metadata to a caller who is not a member of it (read-side isolation)."""
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    pcols = (
        "id, canal_message_id, canal_note_id, nom, taille, type, url, storage_path, cree_le, duree_s, est_audio"
        if att.bucket()
        else "id, canal_message_id, canal_note_id, nom, taille, type, url, cree_le, duree_s, est_audio"
    )
    pieces = db.fetch_all(
        f"SELECT {pcols} FROM collab_piece WHERE canal_message_id = ANY(%s) ORDER BY cree_le", (ids,), role=role
    )
    reponses = db.fetch_all(
        "SELECT id, message_id, auteur_id, corps, cree_le FROM collab_canal_reponse "
        "WHERE message_id = ANY(%s) ORDER BY cree_le",
        (ids,),
        role=role,
    )
    notes = db.fetch_all(
        "SELECT id, canal_message_id, ordre, transcription_brute, transcription_redigee, "
        "transcription_statut, transcription_erreur, audio_conserver, cree_le "
        "FROM collab_canal_note WHERE canal_message_id = ANY(%s) ORDER BY canal_message_id, ordre, cree_le",
        (ids,), role=role,
    )
    liens = db.fetch_all(
        "SELECT id, parent_id, espace_id, sujet, priorite, statut, cree_le FROM collab_canal_message "
        "WHERE parent_id = ANY(%s) ORDER BY cree_le",
        (ids,), role=role,
    )
    parents = db.fetch_all(
        "SELECT id, espace_id, sujet, priorite, statut, cree_le FROM collab_canal_message WHERE id = ANY(%s)",
        ([r["parent_id"] for r in rows if r.get("parent_id")] or [None],), role=role,
    )
    # Read-side isolation: drop any linked instruction (parent or child) that lives in
    # a workspace the caller cannot access, so its subject/status never leaks. Admins
    # (espaces_ok is None) see every link.
    if espaces_ok is not None:
        liens = [x for x in liens if x.get("espace_id") and str(x["espace_id"]) in espaces_ok]
        parents = [x for x in parents if x.get("espace_id") and str(x["espace_id"]) in espaces_ok]
    # Readable workspace name per message, so the canal shows and can filter by
    # which space an instruction belongs to (the row only carries espace_id). Also
    # covers the assigned workspace of the instruction workflow.
    espace_ids = [r["espace_id"] for r in rows if r.get("espace_id")]
    espace_ids += [r["assigne_espace_id"] for r in rows if r.get("assigne_espace_id")]
    espace_noms: dict[str, str] = {}
    if espace_ids:
        for e in db.fetch_all("SELECT id, nom FROM collab_espace WHERE id = ANY(%s)", (espace_ids,), role=role):
            espace_noms[str(e["id"])] = e["nom"]
    # Multiple assignees per instruction, and the taker's readable name.
    assignes_by: dict[str, list[str]] = {}
    for a in db.fetch_all(
        "SELECT message_id, utilisateur_id FROM collab_canal_assignation WHERE message_id = ANY(%s)",
        (ids,), role=role,
    ):
        assignes_by.setdefault(str(a["message_id"]), []).append(str(a["utilisateur_id"]))
    pec_ids = [r["prise_en_charge_par"] for r in rows if r.get("prise_en_charge_par")]
    pec_noms: dict[str, str] = {}
    if pec_ids:
        for u in db.fetch_all(
            "SELECT u.id, COALESCE(m.nom_affiche, u.email) AS nom FROM utilisateur u "
            "LEFT JOIN membre m ON m.id = u.membre_id WHERE u.id = ANY(%s)",
            (pec_ids,), role=role,
        ):
            pec_noms[str(u["id"])] = u["nom"]
    served = att.served_urls(pieces)
    # Audio pieces indexed by the note they belong to; documents stay at message level.
    audio_by_note: dict[str, dict[str, Any]] = {
        str(p["canal_note_id"]): p for p in pieces if p.get("est_audio") and p.get("canal_note_id")
    }
    pieces_by: dict[str, list[dict[str, Any]]] = {}
    for p in pieces:
        if not p.get("est_audio"):
            pieces_by.setdefault(str(p["canal_message_id"]), []).append(p)
    notes_by: dict[str, list[dict[str, Any]]] = {}
    for n in notes:
        notes_by.setdefault(str(n["canal_message_id"]), []).append(n)
    rep_by: dict[str, list[dict[str, Any]]] = {}
    for r in reponses:
        rep_by.setdefault(str(r["message_id"]), []).append(r)
    enfants_by: dict[str, list[dict[str, Any]]] = {}
    for r in liens:
        enfants_by.setdefault(str(r["parent_id"]), []).append(r)
    parent_by = {str(p["id"]): p for p in parents}

    def _lien(row: dict[str, Any]) -> CanalLienOut:
        return CanalLienOut(id=str(row["id"]), sujet=row.get("sujet") or "", priorite=row["priorite"],
                            statut=row["statut"], cree_le=_iso(row.get("cree_le")))

    def _note_out(n: dict[str, Any]) -> CanalNoteOut:
        audio_row = audio_by_note.get(str(n["id"]))
        conserver = bool(n["audio_conserver"])
        expire: int | None = None
        if audio_row and not conserver and audio_row.get("cree_le"):
            expire = max(0, AUDIO_RETENTION_JOURS - (datetime.now(UTC) - audio_row["cree_le"]).days)
        return CanalNoteOut(
            id=str(n["id"]), ordre=int(n["ordre"] or 0),
            audio=_piece_out(audio_row, served) if audio_row else None,
            transcription_brute=n["transcription_brute"] or "",
            transcription_redigee=n["transcription_redigee"] or "",
            transcription_statut=n["transcription_statut"] or "aucune",
            transcription_erreur=n.get("transcription_erreur"),
            audio_conserver=conserver, audio_expire_jours=expire, cree_le=_iso(n.get("cree_le")),
        )

    out: list[CanalMessageOut] = []
    for m in rows:
        mid = str(m["id"])
        note_rows = notes_by.get(mid, [])
        note_outs = [_note_out(n) for n in note_rows]
        first = note_outs[0] if note_outs else None
        parent_row = parent_by.get(str(m["parent_id"])) if m.get("parent_id") else None
        out.append(
            CanalMessageOut(
                id=mid,
                auteur_id=str(m["auteur_id"]) if m["auteur_id"] else "",
                espace_id=str(m["espace_id"]) if m["espace_id"] else None,
                espace_nom=espace_noms.get(str(m["espace_id"])) if m.get("espace_id") else None,
                assigne_id=str(m["assigne_id"]) if m["assigne_id"] else None,
                carte_id=str(m["carte_id"]) if m["carte_id"] else None,
                parent_id=str(m["parent_id"]) if m.get("parent_id") else None,
                categorie=m["categorie"],
                priorite=m["priorite"],
                statut=m["statut"],
                sujet=m["sujet"] or "",
                note=m["note"] or "",
                transcription_brute=first.transcription_brute if first else (m["transcription_brute"] or ""),
                transcription_redigee=first.transcription_redigee if first else (m["transcription_redigee"] or ""),
                transcription_statut=first.transcription_statut if first else (m["transcription_statut"] or "aucune"),
                transcription_erreur=first.transcription_erreur if first else m["transcription_erreur"],
                audio=first.audio if first else None,
                audio_conserver=first.audio_conserver if first else bool(m["audio_conserver"]),
                audio_expire_jours=first.audio_expire_jours if first else None,
                notes=note_outs,
                parent=_lien(parent_row) if parent_row else None,
                enfants=[_lien(r) for r in enfants_by.get(mid, [])],
                pieces=[_piece_out(p, served) for p in pieces_by.get(mid, [])],
                reponses=[
                    CanalReponseOut(
                        id=str(r["id"]), auteur_id=str(r["auteur_id"]) if r["auteur_id"] else "",
                        corps=r["corps"], cree_le=_iso(r["cree_le"]),
                    )
                    for r in rep_by.get(mid, [])
                ],
                cree_le=_iso(m["cree_le"]),
                matricule=m.get("matricule"),
                etape=m.get("etape") or "recue",
                prise_en_charge_par=str(m["prise_en_charge_par"]) if m.get("prise_en_charge_par") else None,
                prise_en_charge_nom=(
                    pec_noms.get(str(m["prise_en_charge_par"])) if m.get("prise_en_charge_par") else None
                ),
                prise_en_charge_le=_iso(m.get("prise_en_charge_le")),
                assigne_espace_id=str(m["assigne_espace_id"]) if m.get("assigne_espace_id") else None,
                assigne_espace_nom=espace_noms.get(str(m["assigne_espace_id"])) if m.get("assigne_espace_id") else None,
                assignes=assignes_by.get(mid, []),
                restitution_rapport=m.get("restitution_rapport"),
                restitution_resume=m.get("restitution_resume"),
                cloture_le=_iso(m.get("cloture_le")),
                cloture_par=str(m["cloture_par"]) if m.get("cloture_par") else None,
            )
        )
    return out


def _fetch(message_id: str, role: str, espaces_ok: set[str] | None = None) -> CanalMessageOut:
    row = db.fetch_one(f"SELECT {_MSG_COLS} FROM collab_canal_message WHERE id = %s", (message_id,), role=role)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    return _assemble([row], role, espaces_ok)[0]


def _espaces_accessibles(user: UserMe) -> list[str]:
    """Workspace ids (general and sub) whose canal this user may read: strictly the
    spaces they belong to, directly or through the parent of a sub-space. No member
    bypass: a back-office role grants no implicit canal access. The sole exception is a
    TECHNICAL super-admin (acces_technique_global), a support identity that reads every
    workspace's canal to maintain the platform. This is the server-side isolation of one
    workspace's instructions from another: the client is never trusted to filter."""
    if user.acces_technique_global:
        rows = db.fetch_all(
            "SELECT id FROM collab_espace WHERE supprime_le IS NULL", (), role=user.role
        )
        return [str(r["id"]) for r in rows]
    rows = db.fetch_all(
        "SELECT e.id FROM collab_espace e WHERE e.supprime_le IS NULL AND ("
        "  EXISTS (SELECT 1 FROM collab_espace_membre m "
        "          WHERE m.espace_id = e.id AND m.utilisateur_id = %s)"
        "  OR EXISTS (SELECT 1 FROM collab_espace_membre pm "
        "             WHERE pm.espace_id = e.parent_id AND pm.utilisateur_id = %s))",
        (user.id, user.id), role=user.role,
    )
    return [str(r["id"]) for r in rows]


def _garde_message(message_id: str, user: UserMe) -> dict[str, Any]:
    """Ensure the caller may act on this instruction, then return its row. Access is
    membership only: EVERYONE, including a super_admin, must belong to the message's
    workspace to touch its content. A back-office role never grants implicit access to
    another workspace's private content. Raises 404 if absent, 403 if not a member."""
    row = db.fetch_one(
        "SELECT espace_id FROM collab_canal_message WHERE id = %s", (message_id,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instruction introuvable")
    esp = str(row["espace_id"]) if row["espace_id"] else None
    if esp is None or esp not in set(_espaces_accessibles(user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="espace non accessible")
    return row


def _verifier_espace_cible(espace_id: str | None, user: UserMe) -> None:
    """Isolation on WRITE: the caller may only place or move an instruction into a
    workspace they belong to (no back-office bypass), and never make it workspace-less.
    Symmetric with the membership-based read scope."""
    if not espace_id or espace_id not in set(_espaces_accessibles(user)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="espace cible non accessible")


def _scope(user: UserMe) -> set[str]:
    """The set of workspace ids a reply/assemble may reveal: strictly the caller's
    memberships, for everyone. Passed to _assemble/_fetch so the parent/child blocks
    never leak the metadata of an instruction in a workspace the caller is not in."""
    return set(_espaces_accessibles(user))


@router.get("/canal", response_model=list[CanalMessageOut])
def list_canal(
    user: Annotated[UserMe, Depends(require_permission("canal.voir"))],
    statut: str | None = None,
    espace_id: str | None = None,
) -> list[CanalMessageOut]:
    # Optional filters compose: a per-space view is the canal of one workspace,
    # a status view the pipeline stage. Both are honoured server side so a large
    # canal does not ship every message to the client just to filter it there.
    clauses: list[str] = []
    params: list[object] = []
    if statut and statut in STATUTS:
        clauses.append("statut = %s")
        params.append(statut)
    # Isolation for EVERYONE (no back-office bypass): a caller only ever sees the
    # instructions of the workspaces they belong to. A requested espace_id must be one
    # of them, otherwise 403; with no espace_id the query is scoped to the accessible
    # set (never a global inbox, even for a super_admin).
    accessibles = _espaces_accessibles(user)
    espaces_ok = set(accessibles)
    if espace_id and espace_id not in espaces_ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="espace non accessible")
    if espace_id:
        clauses.append("espace_id = %s")
        params.append(espace_id)
    else:
        clauses.append("espace_id = ANY(%s)")
        params.append(accessibles)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.fetch_all(
        f"SELECT {_MSG_COLS} FROM collab_canal_message {where} ORDER BY cree_le DESC",
        tuple(params), role=user.role,
    )
    return _assemble(rows, user.role, espaces_ok)


@router.get("/canal/compteur")
def compteur_canal(
    user: Annotated[UserMe, Depends(require_permission("canal.voir"))],
    espace_id: str | None = None,
) -> dict[str, int]:
    # Same membership isolation as the list, for everyone: the unread count never
    # counts another workspace's instructions.
    accessibles = _espaces_accessibles(user)
    if espace_id and espace_id not in set(accessibles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="espace non accessible")
    if espace_id:
        row = db.fetch_one(
            "SELECT count(*) AS n FROM collab_canal_message WHERE statut = 'nouveau' AND espace_id = %s",
            (espace_id,), role=user.role,
        )
    else:
        row = db.fetch_one(
            "SELECT count(*) AS n FROM collab_canal_message WHERE statut = 'nouveau' AND espace_id = ANY(%s)",
            (accessibles,), role=user.role,
        )
    return {"nouveaux": int(row["n"]) if row else 0}


@router.post("/canal", response_model=CanalMessageOut, status_code=status.HTTP_201_CREATED)
def create_canal(
    payload: CanalMessageIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> CanalMessageOut:
    cat = payload.categorie if payload.categorie in CATEGORIES else "autre"
    prio = payload.priorite if payload.priorite in PRIORITES else "normale"
    # Isolation on creation: a non-admin can only create an instruction inside a
    # workspace they belong to (never in another workspace, never workspace-less).
    _verifier_espace_cible(payload.espace_id or None, user)
    created = db.execute(
        "INSERT INTO collab_canal_message (auteur_id, espace_id, categorie, priorite, sujet, note) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (user.id, payload.espace_id or None, cat, prio, payload.sujet or "", payload.note or ""),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="message non cree")
    mid = str(created["id"])
    audit.log(user.id, user.role, "creation_canal_message", "collab_canal_message", mid,
              {"categorie": cat, "priorite": prio})
    return _fetch(mid, user.role, _scope(user))


@router.patch("/canal/{message_id}", response_model=CanalMessageOut)
def update_canal(
    message_id: uuid.UUID,
    payload: CanalPatch,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> CanalMessageOut:
    _garde_message(str(message_id), user)
    fields = payload.model_dump(exclude_unset=True)
    if "categorie" in fields and fields["categorie"] not in CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="categorie invalide")
    if "priorite" in fields and fields["priorite"] not in PRIORITES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="priorite invalide")
    if "statut" in fields and fields["statut"] not in STATUTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="statut invalide")
    # A closed instruction is final: its routing/status cannot be changed through the
    # generic patch either (only descriptive edits and complements remain), so the
    # workflow invariant "no reopening, no reassignment" holds on every path.
    verrou = {"statut", "assigne_id", "espace_id", "parent_id"}
    if verrou & set(fields):
        clot = db.fetch_one(
            "SELECT cloture_le FROM collab_canal_message WHERE id = %s", (str(message_id),), role=user.role,
        )
        if clot and clot.get("cloture_le"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="instruction cloturee: statut et affectation verrouilles (seuls des complements sont possibles)",
            )
    if "parent_id" in fields:
        pid = (fields["parent_id"] or "").strip()
        if pid:
            if pid == str(message_id):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail="un canal ne peut pas se lier a lui-meme")
            if not db.fetch_one("SELECT 1 FROM collab_canal_message WHERE id = %s", (pid,), role=user.role):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="canal parent introuvable")
            # Isolation: the continuity link may only point to a parent the caller can
            # access. Otherwise the parent block returned by _fetch would leak the
            # subject/status of an instruction in a workspace the caller is not in.
            _garde_message(pid, user)
            # Walk the proposed parent's ancestor chain: if this channel appears, the
            # link would create a cycle (any length). Bounded so an existing bad cycle
            # cannot loop forever.
            ancetre: str | None = pid
            for _ in range(64):
                row = db.fetch_one(
                    "SELECT parent_id FROM collab_canal_message WHERE id = %s", (ancetre,), role=user.role
                )
                ancetre = str(row["parent_id"]) if row and row.get("parent_id") else None
                if ancetre is None:
                    break
                if ancetre == str(message_id):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="lien circulaire interdit")
    # Isolation on move: a non-admin can only move an instruction to a workspace they
    # belong to, and never make it workspace-less. Only checked when espace_id changes.
    if "espace_id" in fields:
        cible = (fields["espace_id"] or "").strip() or None
        _verifier_espace_cible(cible, user)
    # Keep assignee consistency in line with the workflow's affecter endpoint: an
    # assignee set through the generic patch must be an existing, active account.
    if fields.get("assigne_id"):
        if not db.fetch_one(
            "SELECT 1 FROM utilisateur WHERE id = %s AND actif = true", (fields["assigne_id"],), role=user.role
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="assigne invalide")
    allowed = ("sujet", "note", "categorie", "priorite", "statut", "assigne_id", "espace_id", "parent_id")
    sets: list[str] = []
    values: list[Any] = []
    for key in allowed:
        if key in fields:
            sets.append(f"{key} = %s")
            # sujet/note keep an empty string; id fields (assigne_id, espace_id,
            # parent_id) treat "" as "clear the reference" (NULL).
            values.append(fields[key] if fields[key] != "" or key in ("sujet", "note") else None)
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rien a modifier")
    updated = db.execute(
        f"UPDATE collab_canal_message SET {', '.join(sets)}, maj_le = now() WHERE id = %s RETURNING id",
        (*values, str(message_id)),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    audit.log(user.id, user.role, "modification_canal_message", "collab_canal_message", str(message_id),
              {"champs": sorted(fields.keys())})
    return _fetch(str(message_id), user.role, _scope(user))


@router.delete("/canal/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_canal(
    message_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> None:
    _garde_message(str(message_id), user)
    bucket = att.bucket()
    if bucket:
        for p in db.fetch_all(
            "SELECT storage_path FROM collab_piece WHERE canal_message_id = %s AND storage_path IS NOT NULL",
            (str(message_id),), role=user.role,
        ):
            try:
                storage.delete_object(bucket, str(p["storage_path"]))
            except (storage.StorageError, OSError):
                pass
    deleted = db.execute(
        "DELETE FROM collab_canal_message WHERE id = %s RETURNING id", (str(message_id),), role=user.role
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    audit.log(user.id, user.role, "suppression_canal_message", "collab_canal_message", str(message_id), {})


def _insert_piece(message_id: str, nom: str, type_: str, data_url: str, est_audio: bool,
                  duree_s: int | None, user: UserMe, note_id: str | None = None) -> dict[str, Any]:
    """Store a channel attachment or audio note: object storage when a bucket is
    configured, else inline. Refuses active-content types and oversized audio. When
    ``note_id`` is given, the audio is tied to that note in the SAME insert (atomic),
    so it can never end up orphaned/unpurgeable with a NULL note link."""
    raw = att.decode_data_url(data_url)
    att.reject_active_content(att.data_url_mime(data_url), type_)
    if est_audio and len(raw) > _AUDIO_MAX:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="audio trop volumineux (max 24 Mo)"
        )
    if not est_audio:
        att.validate_payload(data_url, type_)
    bucket = att.bucket()
    if bucket:
        content_type = att.stored_content_type(att.data_url_mime(data_url), type_)
        piece_id = str(uuid.uuid4())
        path = f"canal/{piece_id}"
        storage.upload_bytes(bucket, path, raw, content_type)
        try:
            created = db.execute(
                "INSERT INTO collab_piece (id, canal_message_id, canal_note_id, nom, taille, type, url, "
                "storage_path, est_audio, duree_s, cree_par) VALUES (%s::uuid, %s, %s, %s, %s, %s, '', %s, %s, %s, %s) "
                "RETURNING id, nom, taille, type, url, storage_path, cree_le, duree_s, est_audio",
                (piece_id, message_id, note_id, nom, len(raw), content_type, path, est_audio, duree_s, user.id),
                role=user.role,
            )
        except Exception:
            storage.delete_object(bucket, path)
            raise
        if not created:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="piece non creee")
        return created
    created = db.execute(
        "INSERT INTO collab_piece (canal_message_id, canal_note_id, nom, taille, type, url, est_audio, duree_s, "
        "cree_par) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id, nom, taille, type, url, cree_le, duree_s, est_audio",
        (message_id, note_id, nom, len(raw), (type_ or "")[:120], data_url, est_audio, duree_s, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="piece non creee")
    return created



@router.post("/canal/{message_id}/pieces", response_model=CanalMessageOut, status_code=status.HTTP_201_CREATED)
def ajouter_piece(
    message_id: uuid.UUID,
    payload: PieceIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> CanalMessageOut:
    mid = str(message_id)
    _garde_message(mid, user)
    _insert_piece(mid, (payload.nom or "piece").strip()[:200], payload.type, payload.data_url, False, None, user)
    audit.log(user.id, user.role, "ajout_piece_canal", "collab_canal_message", mid, {})
    return _fetch(mid, user.role, _scope(user))


@router.delete("/canal/pieces/{piece_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_piece_canal(
    piece_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> None:
    row = db.fetch_one(
        "SELECT storage_path, canal_message_id FROM collab_piece WHERE id = %s AND canal_message_id IS NOT NULL",
        (str(piece_id),), role=user.role,
    )
    if not row:
        return
    _garde_message(str(row["canal_message_id"]), user)
    db.execute("DELETE FROM collab_piece WHERE id = %s", (str(piece_id),), role=user.role)
    if row.get("storage_path") and att.bucket():
        try:
            storage.delete_object(att.bucket(), str(row["storage_path"]))
        except (storage.StorageError, OSError):
            pass
    audit.log(user.id, user.role, "suppression_piece_canal", "collab_canal_message",
              str(row["canal_message_id"]), {})


AUDIO_RETENTION_JOURS = 30


def purge_audio_expire(role: str | None) -> int:
    """Delete channel voice-note audio older than the retention window (30 days),
    unless its note is flagged ``audio_conserver``. The transcription and every other
    element of the note are kept. Returns the number of audio objects purged. Meant to
    be called by the daily job."""
    rows = db.fetch_all(
        "SELECT p.id, p.storage_path FROM collab_piece p "
        "JOIN collab_canal_note n ON n.id = p.canal_note_id "
        "WHERE p.est_audio AND NOT n.audio_conserver "
        f"AND p.cree_le < now() - interval '{AUDIO_RETENTION_JOURS} days'",
        (),
        role=role,
    )
    bucket = att.bucket()
    purges = 0
    for r in rows:
        db.execute("DELETE FROM collab_piece WHERE id = %s", (str(r["id"]),), role=role)
        if r.get("storage_path") and bucket:
            try:
                storage.delete_object(bucket, str(r["storage_path"]))
            except (storage.StorageError, OSError):
                pass
        purges += 1
    return purges


@router.post("/canal/{message_id}/reponses", response_model=CanalMessageOut, status_code=status.HTTP_201_CREATED)
def ajouter_reponse(
    message_id: uuid.UUID,
    payload: ReponseIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> CanalMessageOut:
    mid = str(message_id)
    _garde_message(mid, user)
    db.execute(
        "INSERT INTO collab_canal_reponse (message_id, auteur_id, corps) VALUES (%s, %s, %s)",
        (mid, user.id, payload.corps), role=user.role,
    )
    audit.log(user.id, user.role, "reponse_canal", "collab_canal_message", mid, {})
    return _fetch(mid, user.role, _scope(user))


class CarteDepuisCanalOut(BaseModel):
    carte_id: str


@router.post("/canal/{message_id}/creer-carte", response_model=CarteDepuisCanalOut, status_code=status.HTTP_201_CREATED)
def creer_carte_depuis_canal(
    message_id: uuid.UUID,
    payload: CreerCarteIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> CarteDepuisCanalOut:
    """Turn a channel message into a real card on a chosen board and column. The card
    title is the message subject; its description carries the note and the redaction
    (or the raw text). The message is linked to the card and moved to 'en_cours'."""
    mid = str(message_id)
    _garde_message(mid, user)
    msg = db.fetch_one("SELECT sujet, note FROM collab_canal_message WHERE id = %s", (mid,), role=user.role)
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message introuvable")
    # The transcriptions now live on each note: compose the card body from every note's
    # redaction (or its raw text), in order, so nothing is lost for post-0114 channels.
    notes = db.fetch_all(
        "SELECT transcription_redigee, transcription_brute FROM collab_canal_note "
        "WHERE canal_message_id = %s ORDER BY ordre, cree_le",
        (mid,), role=user.role,
    )
    # Enforce space membership on the TARGET board, exactly like create_carte: the
    # channel is global, but writing a card into a space's board still requires being
    # an active member of that space (no cross-space escalation via this endpoint).
    espace_id = _espace_of_tableau(payload.tableau_id, user.role)
    require_espace_role(espace_id, user, MEMBRES_ACTIFS)
    col = db.fetch_one(
        "SELECT tableau_id FROM collab_colonne WHERE id = %s AND tableau_id = %s",
        (payload.colonne_id, payload.tableau_id), role=user.role,
    )
    if not col:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="colonne invalide pour ce tableau")
    titre = (msg["sujet"] or "Demande du canal").strip()[:200]
    textes = [(n["transcription_redigee"] or n["transcription_brute"] or "").strip() for n in notes]
    corps = "\n\n".join(t for t in textes if t)
    note = msg["note"] or ""
    brut = f"{note}\n\n{corps}".strip() if note and corps and note != corps else (corps or note)
    # The description is a rendered HTML sink (like every card description): sanitise
    # it through the shared allowlist so a note or transcription can never carry
    # active content into another member's session.
    desc = sanitize_html(brut) or ""
    created = db.execute(
        "INSERT INTO collab_carte (tableau_id, colonne_id, titre, description, position, numero, cree_par) "
        "VALUES (%s, %s, %s, %s, "
        "(SELECT coalesce(max(position), -1) + 1 FROM collab_carte WHERE colonne_id = %s), "
        "(SELECT coalesce(max(numero), 0) + 1 FROM collab_carte WHERE tableau_id = %s), %s) RETURNING id",
        (payload.tableau_id, payload.colonne_id, titre, desc, payload.colonne_id, payload.tableau_id, user.id),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="carte non creee")
    cid = str(created["id"])
    db.execute("UPDATE collab_tableau SET compteur_cartes = compteur_cartes + 1 WHERE id = %s",
               (payload.tableau_id,), role=user.role)
    db.execute(
        "UPDATE collab_canal_message SET carte_id = %s, statut = 'en_cours', maj_le = now() WHERE id = %s",
        (cid, mid), role=user.role,
    )
    audit.log(user.id, user.role, "carte_depuis_canal", "collab_carte", cid, {"message_id": mid})
    return CarteDepuisCanalOut(carte_id=cid)
