# ruff: noqa: E501 - long SQL statements
"""Telegram voice-note ingestion into the moderator channel (pull on demand).

Why pull and not a webhook: the bot uses getUpdates polling, and the OTP and
account-linking flows already depend on getUpdates. Telegram forbids a webhook
and getUpdates at once, so a webhook would break those live auth flows. Instead a
moderator triggers this import; it reads the pending bot updates, turns each new
voice/audio message coming from a chat ALREADY LINKED to a member (the allowlist)
into a channel message carrying a voice note, and records the Telegram file id so
a second import never duplicates it (idempotent).
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from . import collaboration_canal as cc
from .collaboration_espaces import GERANTS, require_espace_role
from .config import settings
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/collaboration", tags=["collaboration-telegram-ingest"])

_TELEGRAM_TIMEOUT = 15
_MAX_VOICE_BYTES = cc._AUDIO_MAX
_DEFAULT_MIME = "audio/ogg"


class VoiceItem(BaseModel):
    update_id: int
    chat_id: str
    file_id: str
    duree_s: int | None = None
    mime: str = _DEFAULT_MIME
    nom: str = "note-telegram.ogg"
    legende: str = ""


class ImportResult(BaseModel):
    importees: int
    ignorees_deja_vues: int
    ignorees_non_autorisees: int
    messages: list[str]


def _media_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """The voice or audio payload of a Telegram message, if any."""
    media = message.get("voice") or message.get("audio")
    return media if isinstance(media, dict) else None


def extraire_notes_vocales(
    updates: list[dict[str, Any]],
    chats_autorises: set[str],
    deja_vus: set[str],
) -> list[VoiceItem]:
    """Pure extraction: keep voice/audio messages whose chat is allow-listed (linked
    to a member) and whose Telegram file id was not already ingested. No I/O, so it
    is unit-testable. Dedups within the batch too (same file id can repeat across
    getUpdates calls)."""
    items: list[VoiceItem] = []
    seen_here: set[str] = set()
    for update in updates:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        cid = chat.get("id")
        chat_id = str(cid) if cid is not None else None
        if not chat_id or chat_id not in chats_autorises:
            continue
        media = _media_of(message)
        if not media:
            continue
        file_id = media.get("file_id")
        if not file_id or file_id in deja_vus or file_id in seen_here:
            continue
        seen_here.add(str(file_id))
        items.append(VoiceItem(
            update_id=int(update.get("update_id") or 0),
            chat_id=chat_id,
            file_id=str(file_id),
            duree_s=media.get("duration"),
            mime=str(media.get("mime_type") or _DEFAULT_MIME),
            nom=str(media.get("file_name") or "note-telegram.ogg"),
            legende=str(message.get("caption") or ""),
        ))
    return items


def _get_updates(token: str) -> list[dict[str, Any]]:
    if not token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="telegram non configure")
    # Read-only scan (no offset confirmation), so the OTP/linking flows that also
    # call getUpdates keep seeing the same updates. A single short-poll can return a
    # partial subset of the pending updates, so union a couple of passes by
    # update_id to gather them reliably without ever advancing the offset.
    url = f"{settings.telegram_api_base}/bot{token}/getUpdates?limit=100&timeout=0"
    par_id: dict[int, dict[str, Any]] = {}
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=_TELEGRAM_TIMEOUT) as resp:  # noqa: S310 - fixed Telegram host
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            if par_id:
                break  # keep what we already gathered rather than fail the batch
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="telegram getUpdates indisponible"
            ) from exc
        lot = data.get("result", []) or []
        for u in lot:
            uid = u.get("update_id")
            if uid is not None:
                par_id[int(uid)] = u
    return [par_id[k] for k in sorted(par_id)]


def _download_file(file_id: str, token: str) -> bytes:
    """Resolve a Telegram file id to its bytes via getFile then a file download.

    Every network/parse failure is converted to a 502 (never a bare 500 that would
    abort the whole batch or leak the token-bearing URL in a stack trace). The file
    path returned by Telegram is validated before use (defense in depth against a
    path-traversal in the fixed-host download URL)."""
    info_url = f"{settings.telegram_api_base}/bot{token}/getFile?file_id={urllib.parse.quote(file_id)}"
    try:
        with urllib.request.urlopen(info_url, timeout=_TELEGRAM_TIMEOUT) as resp:  # noqa: S310 - fixed Telegram host
            info = json.load(resp)
        if not info.get("ok") or not (info.get("result") or {}).get("file_path"):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="telegram getFile echoue")
        file_path = str(info["result"]["file_path"])
        if ".." in file_path or file_path.startswith("/") or "://" in file_path:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="telegram file_path invalide")
        safe_path = "/".join(urllib.parse.quote(seg) for seg in file_path.split("/"))
        dl_url = f"{settings.telegram_api_base}/file/bot{token}/{safe_path}"
        with urllib.request.urlopen(dl_url, timeout=_TELEGRAM_TIMEOUT) as resp:  # noqa: S310 - fixed Telegram host
            raw = resp.read(_MAX_VOICE_BYTES + 1)
    except HTTPException:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="telechargement telegram indisponible"
        ) from exc
    if len(raw) > _MAX_VOICE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="note vocale trop volumineuse")
    return raw


def _traiter_liaisons_espace(updates: list[dict[str, Any]], par_chat: dict[str, dict[str, Any]], role: str) -> None:
    """Bind a member's chat to a workspace when they open its deep link.

    The workspace deep link makes Telegram send '/start canal_<token>' to the bot.
    For a linked member, resolve the workspace by that token and record it on the
    member so their subsequent voice notes route to that workspace's channel. Only
    already-linked chats are honoured (the allowlist), and an unknown token is
    ignored."""
    prefix = "/start canal_"
    for update in updates:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not text.startswith(prefix):
            continue
        chat = message.get("chat") or {}
        cid = chat.get("id")
        chat_id = str(cid) if cid is not None else None
        lien = par_chat.get(chat_id) if chat_id else None
        token = text[len(prefix):].strip()
        if not lien or not token:
            continue
        esp = db.fetch_one("SELECT id FROM collab_espace WHERE telegram_intake_token = %s", (token,), role=role)
        if not esp:
            continue
        db.execute(
            "UPDATE membre SET telegram_espace_id = %s WHERE id = %s",
            (str(esp["id"]), str(lien["membre_id"])), role=role,
        )
        lien["telegram_espace_id"] = esp["id"]  # reflect for the current run


@router.post("/canal/telegram/importer", response_model=ImportResult)
def importer_notes_telegram(
    user: Annotated[UserMe, Depends(require_permission("canal.traiter"))],
    espace_id: str | None = None,
) -> ImportResult:
    """Pull voice notes sent to the bot and turn each new one into a channel message.

    Allowlist: only chats already linked to a member are accepted. Idempotent and
    race-safe: the Telegram file id is claimed in the ledger first, so a concurrent
    import cannot create a duplicate channel. Optional ``espace_id`` routes the
    created channels to one workspace so a per-space canal stays coherent."""
    # Configured workspaces, each with its OWN dedicated instruction bot (never the
    # member notification bot). A note received by a workspace's bot belongs to it.
    # When ``espace_id`` is given (the user is inside one workspace and wants its notes
    # to surface near-instantly), poll ONLY that workspace's bot: fewer Telegram calls
    # and no getUpdates conflict with the slower global sweep.
    filtre = "AND id = %s " if espace_id else ""
    params: tuple[object, ...] = (espace_id,) if espace_id else ()
    espaces_bot = db.fetch_all(
        "SELECT id, nom, instruction_bot_token FROM collab_espace "
        "WHERE parent_id IS NULL AND instruction_bot_statut = 'configure' AND instruction_bot_token IS NOT NULL "
        "AND NOT archive AND supprime_le IS NULL " + filtre,
        params, role=user.role,
    )
    if not espaces_bot:
        return ImportResult(importees=0, ignorees_deja_vues=0, ignorees_non_autorisees=0,
                            messages=["Aucun bot d'instruction configure. L'equipe back-office doit configurer le bot dedie de l'espace."])
    liens = db.fetch_all(
        "SELECT m.id AS membre_id, m.telegram_chat_id AS chat_id, m.prenoms, m.nom, u.id AS utilisateur_id "
        "FROM membre m LEFT JOIN utilisateur u ON u.membre_id = m.id WHERE m.telegram_chat_id IS NOT NULL",
        (), role=user.role,
    )
    par_chat = {str(r["chat_id"]): r for r in liens}
    autorises = set(par_chat.keys())
    emetteurs_ok = {
        (str(r["espace_id"]), str(r["utilisateur_id"]))
        for r in db.fetch_all("SELECT espace_id, utilisateur_id FROM collab_espace_emetteur", (), role=user.role)
    }

    importees = deja_vues = non_autorisees = 0
    msgs: list[str] = []
    from .collaboration_canal_config import dechiffrer_token
    for esp in espaces_bot:
        eid = str(esp["id"])
        token = dechiffrer_token(esp["instruction_bot_token"])
        try:
            updates = _get_updates(token)
        except HTTPException:
            # Record the failure on the workspace so the back-office shows a real health
            # state (last error + time), never a bare 'configured' that hides a dead bot.
            db.execute(
                "UPDATE collab_espace SET instruction_dernier_echec = %s, instruction_dernier_echec_le = now() WHERE id = %s",
                ("bot d'instruction injoignable (Telegram getUpdates)", eid), role=user.role,
            )
            msgs.append(f"{esp['nom']}: bot d'instruction injoignable")
            continue
        # Successful poll: stamp the last sync and clear a previous error.
        db.execute(
            "UPDATE collab_espace SET instruction_derniere_synchro = now(), instruction_dernier_echec = NULL, "
            "instruction_dernier_echec_le = NULL WHERE id = %s",
            (eid,), role=user.role,
        )
        items = extraire_notes_vocales(updates, autorises, set())
        for item in items:
            lien = par_chat[item.chat_id]
            auteur_id = str(lien["utilisateur_id"]) if lien.get("utilisateur_id") else user.id
            nom_membre = f"{(lien.get('prenoms') or '').strip()} {(lien.get('nom') or '').strip()}".strip() or "membre"
            # The note routes to THIS workspace (its bot received it) only if the
            # sender is an authorized emitter of it; else it is refused (the bot is
            # reserved to authorized emitters). The refusal is surfaced with a precise
            # cause so the failure is never silent (the admin knows exactly what to do).
            if (eid, auteur_id) not in emetteurs_ok:
                non_autorisees += 1
                msgs.append(
                    f"{esp['nom']}: note de {nom_membre} refusee (emetteur non autorise pour cet espace). "
                    "Autorisez-le dans Back-office > Espaces collaboration > Bots et Telegram."
                )
                continue
            statut, detail = _ingerer_note(item, eid, auteur_id, nom_membre, token, user)
            if statut == "importee":
                importees += 1
                db.execute("UPDATE collab_espace SET instruction_derniere_note = now() WHERE id = %s", (eid,), role=user.role)
            elif statut == "deja":
                deja_vues += 1
            else:
                msgs.append(f"{nom_membre}: {detail}")
    audit.log(user.id, user.role, "import_notes_telegram", "collab_canal_message", None,
              {"importees": importees, "espaces": len(espaces_bot)})
    return ImportResult(importees=importees, ignorees_deja_vues=deja_vues,
                        ignorees_non_autorisees=non_autorisees, messages=msgs)


def _ingerer_note(item: VoiceItem, espace_id: str, auteur_id: str, nom_membre: str, token: str, user: UserMe) -> tuple[str, str]:
    """Claim, download and store one voice note as a channel message in the workspace
    instruction space. Idempotent via the file-id ledger. Returns (status, detail)."""
    claim = db.execute(
        "INSERT INTO telegram_voice_ingest (telegram_file_id, telegram_update_id, chat_id, membre_id, importe_par) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (telegram_file_id) DO NOTHING RETURNING id",
        (item.file_id, item.update_id, item.chat_id, None, user.id), role=user.role,
    )
    if not claim:
        return ("deja", "")
    ledger_id = str(claim["id"])
    mid: str | None = None
    try:
        raw = _download_file(item.file_id, token)
        mime = item.mime if item.mime.startswith("audio/") else _DEFAULT_MIME
        data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        sujet = (item.legende.strip() or f"Note vocale Telegram de {nom_membre}")[:200]
        created = db.execute(
            "INSERT INTO collab_canal_message (auteur_id, espace_id, categorie, priorite, sujet, note) "
            "VALUES (%s, %s, 'autre', 'normale', %s, %s) RETURNING id",
            (auteur_id, espace_id, sujet, f"Recu via Telegram de {nom_membre}."), role=user.role,
        )
        mid = str(created["id"])
        note = db.execute("INSERT INTO collab_canal_note (canal_message_id, ordre) VALUES (%s, 0) RETURNING id", (mid,), role=user.role)
        note_id = str(note["id"])
        cc._insert_piece(mid, item.nom[:200], mime, data_url, True, item.duree_s, user, note_id=note_id)
        # telegram_chat_id is unique per member (0140), so this resolves to at most one row;
        # LIMIT 1 keeps the scalar subquery deterministic even if that invariant regressed.
        db.execute("UPDATE telegram_voice_ingest SET canal_message_id = %s, canal_note_id = %s, membre_id = "
                   "(SELECT id FROM membre WHERE telegram_chat_id = %s LIMIT 1) WHERE id = %s",
                   (mid, note_id, item.chat_id, ledger_id), role=user.role)
        return ("importee", "")
    except Exception as exc:  # noqa: BLE001 - one bad note must not abort the batch
        if mid:
            db.execute("DELETE FROM collab_canal_message WHERE id = %s", (mid,), role=user.role)
        db.execute("DELETE FROM telegram_voice_ingest WHERE id = %s AND canal_message_id IS NULL", (ledger_id,), role=user.role)
        return ("echec", exc.detail if isinstance(exc, HTTPException) else "note non importee")


# --- Authorized instruction emitters per workspace ------------------------------


class EmetteurIn(BaseModel):
    utilisateur_id: str


def _emetteurs_max(role: str) -> int:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = 'canal_emetteurs_max'", (), role=role)
    try:
        return max(1, int(str(row["valeur"]).strip('"'))) if row and row["valeur"] is not None else 1
    except (TypeError, ValueError):
        return 1


@router.get("/espaces/{espace_id}/emetteurs")
def liste_emetteurs(
    espace_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.superviser"))],
) -> dict[str, Any]:
    eid = str(espace_id)
    rows = db.fetch_all(
        "SELECT e.utilisateur_id, COALESCE(m.nom_affiche, u.email) AS nom "
        "FROM collab_espace_emetteur e JOIN utilisateur u ON u.id = e.utilisateur_id "
        "LEFT JOIN membre m ON m.id = u.membre_id WHERE e.espace_id = %s ORDER BY e.ajoute_le",
        (eid,), role=user.role,
    )
    return {
        "max": _emetteurs_max(user.role),
        "emetteurs": [{"utilisateur_id": str(r["utilisateur_id"]), "nom": r["nom"]} for r in rows],
    }


@router.post("/espaces/{espace_id}/emetteurs")
def ajouter_emetteur(
    espace_id: uuid.UUID,
    payload: EmetteurIn,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, Any]:
    eid = str(espace_id)
    require_espace_role(eid, user, GERANTS)
    if not db.fetch_one("SELECT 1 FROM utilisateur WHERE id = %s AND actif = true", (payload.utilisateur_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="utilisateur invalide")
    n = db.fetch_one("SELECT count(*) AS n FROM collab_espace_emetteur WHERE espace_id = %s", (eid,), role=user.role)
    deja = db.fetch_one(
        "SELECT 1 FROM collab_espace_emetteur WHERE espace_id = %s AND utilisateur_id = %s",
        (eid, payload.utilisateur_id), role=user.role,
    )
    if not deja and int((n or {}).get("n", 0)) >= _emetteurs_max(user.role):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"nombre maximum d'emetteurs atteint ({_emetteurs_max(user.role)}); augmentez la limite en back-office ou retirez un emetteur",
        )
    db.execute(
        "INSERT INTO collab_espace_emetteur (espace_id, utilisateur_id, ajoute_par) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (eid, payload.utilisateur_id, user.id), role=user.role,
    )
    audit.log(user.id, user.role, "canal_emetteur_ajout", "collab_espace", eid, {"utilisateur_id": payload.utilisateur_id})
    return liste_emetteurs(espace_id, user)


@router.delete("/espaces/{espace_id}/emetteurs/{utilisateur_id}")
def retirer_emetteur(
    espace_id: uuid.UUID,
    utilisateur_id: uuid.UUID,
    user: Annotated[UserMe, Depends(require_permission("collaboration.gerer"))],
) -> dict[str, Any]:
    eid = str(espace_id)
    require_espace_role(eid, user, GERANTS)
    db.execute(
        "DELETE FROM collab_espace_emetteur WHERE espace_id = %s AND utilisateur_id = %s",
        (eid, str(utilisateur_id)), role=user.role,
    )
    audit.log(user.id, user.role, "canal_emetteur_retrait", "collab_espace", eid, {"utilisateur_id": str(utilisateur_id)})
    return liste_emetteurs(espace_id, user)
