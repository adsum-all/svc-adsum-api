"""Unit tests for the pure extraction of Telegram voice notes.

The extraction is the ingestion's decision core: which Telegram updates become
channel voice notes. It is pure (no network, no DB), so it is fully testable with
realistic getUpdates payloads.
"""
from __future__ import annotations

from app.collaboration_telegram_ingest import extraire_notes_vocales


def _voice_update(update_id: int, chat_id: int, file_id: str, *, caption: str = "", duration: int = 5) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
            "date": 1_700_000_000,
            "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
            "caption": caption,
        },
    }


def _text_update(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "chat": {"id": chat_id, "type": "private"}, "text": text},
    }


def test_keeps_only_allowlisted_chats() -> None:
    updates = [
        _voice_update(1, 111, "AAA"),  # allow-listed
        _voice_update(2, 999, "BBB"),  # not linked -> ignored
    ]
    items = extraire_notes_vocales(updates, chats_autorises={"111"}, deja_vus=set())
    assert [i.file_id for i in items] == ["AAA"]
    assert items[0].chat_id == "111"


def test_skips_already_ingested_file_ids() -> None:
    updates = [_voice_update(1, 111, "AAA"), _voice_update(2, 111, "CCC")]
    items = extraire_notes_vocales(updates, chats_autorises={"111"}, deja_vus={"AAA"})
    assert [i.file_id for i in items] == ["CCC"]


def test_dedups_within_batch() -> None:
    updates = [_voice_update(1, 111, "AAA"), _voice_update(2, 111, "AAA")]
    items = extraire_notes_vocales(updates, chats_autorises={"111"}, deja_vus=set())
    assert [i.file_id for i in items] == ["AAA"]


def test_ignores_non_voice_messages() -> None:
    updates = [_text_update(1, 111, "/start token"), _voice_update(2, 111, "AAA")]
    items = extraire_notes_vocales(updates, chats_autorises={"111"}, deja_vus=set())
    assert [i.file_id for i in items] == ["AAA"]


def test_carries_caption_and_duration() -> None:
    updates = [_voice_update(7, 111, "AAA", caption="Reunion mardi", duration=42)]
    items = extraire_notes_vocales(updates, chats_autorises={"111"}, deja_vus=set())
    assert items[0].legende == "Reunion mardi"
    assert items[0].duree_s == 42
    assert items[0].mime == "audio/ogg"


def test_empty_when_no_allowlist() -> None:
    updates = [_voice_update(1, 111, "AAA")]
    assert extraire_notes_vocales(updates, chats_autorises=set(), deja_vus=set()) == []
