"""Moderator channel, per-note operations.

A channel (collab_canal_message) can hold several voice notes (collab_canal_note),
each with its own audio, verbatim transcription and professional redaction. These
endpoints add a note to an existing channel, transcribe/redraft a single note, edit
its texts and delete it. They live in their own module to keep collaboration_canal
under the file-size limit; they reuse its helpers (storage, insert, fetch, and the
per-workspace access guard). Writes are guarded by ``canal.traiter`` and isolated to
the caller's workspaces via ``cc._garde_message``.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import ai_providers, audit, db, storage
from . import attachments_core as att
from . import collaboration_canal as cc
from .fields import TextStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-canal-notes"])


class NotePatch(BaseModel):
    transcription_brute: TextStr | None = None
    transcription_redigee: TextStr | None = None
    audio_conserver: bool | None = None


def _note_row(note_id: str, user: UserMe) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT id, canal_message_id, ordre FROM collab_canal_note WHERE id = %s", (note_id,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note introuvable")
    # Isolation: a note belongs to a channel message, itself belonging to a workspace.
    # A non-admin may only reach a note of a workspace they belong to.
    cc._garde_message(str(row["canal_message_id"]), user)
    return row


def _note_audio_bytes(note_id: str, role: str) -> tuple[bytes, str, str]:
    """Raw bytes, filename and mime of a note's audio."""
    cols = "id, nom, type, url, storage_path" if att.bucket() else "id, nom, type, url"
    row = db.fetch_one(
        f"SELECT {cols} FROM collab_piece WHERE canal_note_id = %s AND est_audio LIMIT 1", (note_id,), role=role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="aucune note vocale a transcrire")
    mime = row.get("type") or "audio/webm"
    if row.get("storage_path") and att.bucket():
        raw = storage.download_bytes(att.bucket(), str(row["storage_path"]), max_bytes=cc._AUDIO_MAX)
    else:
        raw = att.decode_data_url(row["url"] or "")
    return raw, str(row["nom"] or "note.webm"), mime


@router.post("/canal/{message_id}/notes", response_model=cc.CanalMessageOut, status_code=status.HTTP_201_CREATED)
def ajouter_note(
    message_id: uuid.UUID,
    payload: cc.PieceIn,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Add a voice note to an existing channel: create the note, then store its audio.
    If the audio is rejected (too large / wrong type) the empty note is rolled back."""
    mid = str(message_id)
    cc._garde_message(mid, user)
    nrow = db.fetch_one(
        "SELECT coalesce(max(ordre), -1) + 1 AS n FROM collab_canal_note WHERE canal_message_id = %s",
        (mid,), role=user.role,
    )
    ordre = int(nrow["n"]) if nrow else 0
    created = db.execute(
        "INSERT INTO collab_canal_note (canal_message_id, ordre) VALUES (%s, %s) RETURNING id",
        (mid, ordre), role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="note non creee")
    note_id = str(created["id"])
    try:
        cc._insert_piece(mid, (payload.nom or "note-vocale").strip()[:200], payload.type or "audio/webm",
                         payload.data_url, True, payload.duree_s, user, note_id=note_id)
    except Exception:
        db.execute("DELETE FROM collab_canal_note WHERE id = %s", (note_id,), role=user.role)
        raise
    audit.log(user.id, user.role, "ajout_note_canal", "collab_canal_message", mid, {"note_id": note_id, "ordre": ordre})
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/notes/{note_id}/transcrire", response_model=cc.CanalMessageOut)
def transcrire_note(
    note_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Transcribe one note verbatim, draft its professional redaction, and (only for the
    FIRST note of the channel) auto-fill the channel title/category/priority when they
    were left empty. Any failure is recorded as 'echec', never stuck at 'en_cours'."""
    nid = str(note_id)
    n = _note_row(nid, user)
    mid = str(n["canal_message_id"])
    ordre = int(n["ordre"] or 0)
    raw, filename, mime = _note_audio_bytes(nid, user.role)
    db.execute(
        "UPDATE collab_canal_note SET transcription_statut = 'en_cours', transcription_erreur = NULL WHERE id = %s",
        (nid,), role=user.role,
    )
    try:
        brut = ai_providers.transcrire(raw, filename, mime, user.role)
        redige = ""
        titre = categorie = priorite = None
        try:
            analyse = ai_providers.analyser_note(brut, user.role)
            redige = analyse["redige"]
            if ordre == 0:
                msg = db.fetch_one(
                    "SELECT sujet, categorie, priorite FROM collab_canal_message WHERE id = %s", (mid,), role=user.role
                ) or {}
                if not (msg.get("sujet") or "").strip() and analyse["titre"]:
                    titre = analyse["titre"]
                if (msg.get("categorie") or "autre") == "autre" and analyse["categorie"] != "autre":
                    categorie = analyse["categorie"]
                if (msg.get("priorite") or "normale") == "normale" and analyse["priorite"] != "normale":
                    priorite = analyse["priorite"]
        except ai_providers.AIError:
            redige = ""
        db.execute(
            "UPDATE collab_canal_note SET transcription_brute = %s, transcription_redigee = %s, "
            "transcription_statut = 'prete', transcription_erreur = NULL, maj_le = now() WHERE id = %s",
            (brut, redige, nid), role=user.role,
        )
        auto = (("sujet", titre), ("categorie", categorie), ("priorite", priorite))
        sets = [f"{c} = %s" for c, v in auto if v is not None]
        vals = [v for _c, v in auto if v is not None]
        if sets:
            db.execute(
                f"UPDATE collab_canal_message SET {', '.join(sets)}, maj_le = now() WHERE id = %s",
                (*vals, mid), role=user.role,
            )
        audit.log(user.id, user.role, "transcription_note_canal", "collab_canal_note", nid, {"caracteres": len(brut)})
    except (ai_providers.AIError, storage.StorageError, OSError) as exc:
        db.execute(
            "UPDATE collab_canal_note SET transcription_statut = 'echec', transcription_erreur = %s WHERE id = %s",
            (str(exc)[:400], nid), role=user.role,
        )
    return cc._fetch(mid, user.role, cc._scope(user))


@router.post("/canal/notes/{note_id}/rediger", response_model=cc.CanalMessageOut)
def rediger_note(
    note_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Re-run the professional redaction of one note from its (possibly hand-corrected)
    raw transcription, without touching the raw text."""
    nid = str(note_id)
    n = _note_row(nid, user)
    mid = str(n["canal_message_id"])
    row = db.fetch_one("SELECT transcription_brute FROM collab_canal_note WHERE id = %s", (nid,), role=user.role)
    brut = ((row or {}).get("transcription_brute") or "").strip()
    if not brut:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="aucun texte brut a rediger")
    try:
        redige, _problemes = ai_providers.rediger(brut, user.role)
        db.execute(
            "UPDATE collab_canal_note SET transcription_redigee = %s, transcription_statut = 'prete', "
            "transcription_erreur = NULL, maj_le = now() WHERE id = %s",
            (redige, nid), role=user.role,
        )
        audit.log(user.id, user.role, "redaction_note_canal", "collab_canal_note", nid, {})
    except ai_providers.AIError as exc:
        db.execute(
            "UPDATE collab_canal_note SET transcription_erreur = %s WHERE id = %s",
            (str(exc)[:400], nid), role=user.role,
        )
    return cc._fetch(mid, user.role, cc._scope(user))


@router.patch("/canal/notes/{note_id}", response_model=cc.CanalMessageOut)
def patch_note(
    note_id: uuid.UUID,
    payload: NotePatch,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> cc.CanalMessageOut:
    """Edit a note's raw transcription, its redaction, or its audio-retention flag."""
    nid = str(note_id)
    n = _note_row(nid, user)
    fields = payload.model_dump(exclude_unset=True)
    allowed = ("transcription_brute", "transcription_redigee", "audio_conserver")
    sets = [f"{k} = %s" for k in allowed if k in fields]
    vals = [fields[k] for k in allowed if k in fields]
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rien a modifier")
    db.execute(
        f"UPDATE collab_canal_note SET {', '.join(sets)}, maj_le = now() WHERE id = %s", (*vals, nid), role=user.role
    )
    audit.log(user.id, user.role, "modification_note_canal", "collab_canal_note", nid,
              {"champs": sorted(fields.keys())})
    return cc._fetch(str(n["canal_message_id"]), user.role, cc._scope(user))


@router.delete("/canal/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
) -> None:
    """Delete one note and its audio object (the channel and its other notes remain)."""
    nid = str(note_id)
    n = db.fetch_one("SELECT canal_message_id FROM collab_canal_note WHERE id = %s", (nid,), role=user.role)
    if not n:
        return
    cc._garde_message(str(n["canal_message_id"]), user)
    for p in db.fetch_all("SELECT id, storage_path FROM collab_piece WHERE canal_note_id = %s", (nid,), role=user.role):
        db.execute("DELETE FROM collab_piece WHERE id = %s", (str(p["id"]),), role=user.role)
        if p.get("storage_path") and att.bucket():
            try:
                storage.delete_object(att.bucket(), str(p["storage_path"]))
            except (storage.StorageError, OSError):
                pass
    db.execute("DELETE FROM collab_canal_note WHERE id = %s", (nid,), role=user.role)
    audit.log(user.id, user.role, "suppression_note_canal", "collab_canal_message",
              str(n["canal_message_id"]), {"note_id": nid})
