# ruff: noqa: E501
"""Neural text-to-speech for member-facing contents.

The active ``tts`` provider (Reglages IA, key Fernet-encrypted) synthesises the
CLEANED text: markup markers, URLs and emojis are stripped first, so the voice
reads the content faithfully and never describes pictograms. Every synthesis is
cached by content hash (``tts_cache``), so a frequently listened Information is
billed once. When no provider is configured the endpoint answers 503 and the
member app falls back to the device's native voice (with the same cleaned text).

Voices: the member chooses a male or female voice; the language follows their
profile language (French or English). Dioula and Baoule are NOT offered because
no supported neural provider currently publishes such voices; the language field
is extensible the day one does.
"""
from __future__ import annotations

import base64
import hashlib
import json as _json
import re
import urllib.error
import urllib.request
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import db
from .ai_providers import AIError, _cf_url, load_active
from .auth import current_user
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["tts"])

_TIMEOUT = 60

# Pictograms and joiners a voice must never describe: emojis, symbols, variation
# selectors, flags, zero-width joiners. Built from code points so the source file
# stays pure ASCII (the project bans literal emojis in code).
_EMOJI_RANGES = (
    (0x1F000, 0x1FBFF),  # emoji, symbols, flags, extended pictographs
    (0x2190, 0x21FF),    # arrows
    (0x2300, 0x27BF),    # technical, dingbats (incl. 0x2764 heart, 0x26EA, 0x2705)
    (0x2B00, 0x2BFF),    # arrows and stars (0x2B50)
    (0xFE00, 0xFE0F),    # variation selectors
    (0x200D, 0x200D),    # zero-width joiner
)
_EMOJI_RE = re.compile(
    "[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _EMOJI_RANGES) + "]+",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"https?://\S+")


def nettoyer_pour_voix(texte: str) -> str:
    """The exact text a voice should read: content only. Strips the light markup
    markers (**gras**, __souligne__, *italique*), URLs and every pictogram, then
    collapses whitespace runs while keeping sentence flow."""
    s = texte or ""
    s = _URL_RE.sub("", s)
    s = s.replace("**", "").replace("__", "").replace("*", "")
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# Genre -> provider voice. OpenAI voices are multilingual and natural in French.
_OPENAI_VOIX = {"homme": "onyx", "femme": "nova"}


def _http_audio(url: str, headers: dict[str, str], body: dict[str, Any]) -> bytes:
    """POST JSON, return raw audio bytes (OpenAI/ElevenLabs speech endpoints)."""
    req = urllib.request.Request(url, data=_json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc


def synthese(texte: str, genre: str, langue: str, role: str) -> tuple[str, bytes]:
    """Synthesise the cleaned text with the active TTS provider. Returns (mime, bytes)."""
    cfg = load_active("tts", role)
    if not cfg:
        raise AIError("aucun fournisseur de synthese vocale actif")
    clean = nettoyer_pour_voix(texte)[:4000]
    if not clean:
        raise AIError("texte vide apres nettoyage")
    fournisseur = str(cfg["fournisseur"])
    cle = str(cfg.get("cle") or "")
    params = cfg.get("params") or {}

    if fournisseur == "openai":
        if not cle:
            raise AIError("cle OpenAI absente")
        url = (cfg.get("endpoint") or "").strip() or "https://api.openai.com/v1/audio/speech"
        voice = str(params.get(f"voice_{genre}") or _OPENAI_VOIX.get(genre) or "nova")
        audio = _http_audio(url, {"Authorization": f"Bearer {cle}"}, {
            "model": cfg.get("modele") or "gpt-4o-mini-tts",
            "voice": voice,
            "input": clean,
            "response_format": "mp3",
        })
        return "audio/mpeg", audio

    if fournisseur == "elevenlabs":
        if not cle:
            raise AIError("cle ElevenLabs absente")
        voice_id = str(params.get(f"voice_{genre}") or "").strip()
        if not voice_id:
            raise AIError(f"identifiant de voix ElevenLabs manquant (params.voice_{genre})")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        audio = _http_audio(url, {"xi-api-key": cle}, {
            "text": clean,
            "model_id": cfg.get("modele") or "eleven_multilingual_v2",
        })
        return "audio/mpeg", audio

    if fournisseur == "cloudflare":
        if not cle:
            raise AIError("jeton Cloudflare absent")
        out = _http_audio_cf(_cf_url(cfg), {"Authorization": f"Bearer {cle}"}, {
            "prompt": clean,
            "lang": "en" if langue == "en" else "fr",
        })
        return "audio/mpeg", out

    raise AIError(f"fournisseur tts non pris en charge: {fournisseur}")


def _http_audio_cf(url: str, headers: dict[str, str], body: dict[str, Any]) -> bytes:
    """Cloudflare MeloTTS returns JSON {result: {audio: base64}}."""
    req = urllib.request.Request(url, data=_json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            data = _json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc
    b64 = ((data or {}).get("result") or {}).get("audio")
    if not b64:
        raise AIError("reponse MeloTTS sans audio")
    return base64.b64decode(b64)


class TtsIn(BaseModel):
    texte: str = Field(min_length=1, max_length=8000)
    genre: str = Field(default="femme", pattern="^(homme|femme)$")


@router.post("/membres/me/tts")
def tts_membre(payload: TtsIn, user: Annotated[UserMe, Depends(current_user)]) -> dict[str, Any]:
    """Synthesised reading of a content for the connected member, cached by hash."""
    langue = "fr"
    if user.membre_id:
        row = db.fetch_one("SELECT langue FROM membre WHERE id = %s", (user.membre_id,), role=user.role)
        if row and str(row.get("langue") or "").startswith("en"):
            langue = "en"
    cfg = load_active("tts", user.role)
    if not cfg:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="synthese vocale neurale non configurée")
    clean = nettoyer_pour_voix(payload.texte)[:4000]
    empreinte = hashlib.sha256(
        f"{cfg['fournisseur']}|{cfg.get('modele')}|{payload.genre}|{langue}|{clean}".encode()
    ).hexdigest()
    hit = db.fetch_one("SELECT mime, audio FROM tts_cache WHERE cle_hash = %s", (empreinte,), role=user.role)
    if hit:
        return {"mime": hit["mime"], "audio": base64.b64encode(bytes(hit["audio"])).decode(), "cache": True}
    try:
        mime, audio = synthese(payload.texte, payload.genre, langue, user.role)
    except AIError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    db.execute(
        "INSERT INTO tts_cache (cle_hash, mime, audio) VALUES (%s, %s, %s) ON CONFLICT (cle_hash) DO NOTHING",
        (empreinte, mime, audio), role=user.role,
    )
    return {"mime": mime, "audio": base64.b64encode(audio).decode(), "cache": False}
