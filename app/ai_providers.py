"""Pluggable AI providers for the moderator channel: speech-to-text and redaction.

Providers are configured in the ``ai_provider_config`` table and swapped from the
back office in one click, so no key or model name lives in code. A key is stored
Fernet-encrypted and only ever decrypted here, server side, right before the call.

Two capabilities:
- ``stt`` turns a voice note into a VERBATIM raw text (the source of truth);
- ``llm`` turns that raw text into a professional redaction under strict, anti
  hallucination guardrails (never add, drop or alter a fact, name, number or date).

Most providers speak the OpenAI HTTP shape (OpenAI, Groq, Perplexity, a self hosted
server, Azure OpenAI with a different path); Google Gemini has its own shape. All
are wired here. HTTP uses urllib (httpx is unreliable on the Vercel runtime, see
storage.py). Nothing is transcribed unless an active provider is configured, in
which case a clear, typed error is raised for the caller to surface.
"""
from __future__ import annotations

import base64
import json as _json
import re
import secrets
import urllib.error
import urllib.request
from typing import Any

from . import crypto, db

TIMEOUT = 60

# Default full endpoints for the OpenAI-compatible providers. A row's ``endpoint``
# overrides the base; Azure and Gemini are computed from the row.
_CHAT_URL = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "perplexity": "https://api.perplexity.ai/chat/completions",
}
_STT_URL = {
    "openai": "https://api.openai.com/v1/audio/transcriptions",
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
}

_REDACTION_SYSTEM = (
    "Tu es un assistant de redaction professionnelle. On te donne la transcription BRUTE et "
    "verbatim d'une note vocale (ou d'un texte dicte). Ta seule tache: en produire une version "
    "REDIGEE, claire, professionnelle et bien structuree.\n"
    "REGLES ABSOLUES, sans exception:\n"
    "1. N'AJOUTE aucune information, fait, nom, chiffre, date, montant, lieu, decision ou detail "
    "qui ne soit pas deja present dans le texte brut. Aucune invention, aucune extrapolation.\n"
    "2. Ne SUPPRIME aucun element de contenu presente par l'auteur.\n"
    "3. Ne DEFORME rien: garde le sens exact.\n"
    "4. Tu peux UNIQUEMENT: corriger l'orthographe et la grammaire, structurer en phrases, "
    "paragraphes ou listes, retirer les hesitations orales (euh, hum, repetitions involontaires), "
    "et ameliorer la ponctuation.\n"
    "5. Conserve TOUS les nombres, dates, noms propres et montants EXACTEMENT tels quels.\n"
    "6. Si un passage est inaudible ou ambigu, garde-le tel quel ou marque [inaudible]; n'invente "
    "jamais de contenu pour combler un trou.\n"
    "7. Reponds UNIQUEMENT avec le texte redige, sans preambule, sans commentaire, sans guillemets "
    "d'encadrement. Langue: francais."
)


class AIError(RuntimeError):
    """A provider is not configured or a provider call failed."""


def _http_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    data = _json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc
    try:
        return _json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AIError("provider returned invalid JSON") from exc


def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    """GET + parse JSON, used for lightweight auth/connectivity probes (models list).
    A browser-like User-Agent is set because some provider APIs sit behind a WAF
    (e.g. Cloudflare) that rejects the bare Python-urllib signature with a 403/1010."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; ADSUM/1.0; +https://adsum-api.vercel.app)")
    req.add_header("Accept", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc
    try:
        return _json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AIError("provider returned invalid JSON") from exc


def _http_binary(url: str, headers: dict[str, str], content: bytes, content_type: str) -> Any:
    """POST raw bytes (e.g. audio for Cloudflare Workers AI Whisper) and parse JSON."""
    req = urllib.request.Request(url, data=content, method="POST")
    req.add_header("Content-Type", content_type)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc
    try:
        return _json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AIError("provider returned invalid JSON") from exc


def _cf_url(cfg: dict[str, Any]) -> str:
    """Cloudflare Workers AI run URL: needs the account id (in params.account_id) and
    the model id (the row's 'modele', e.g. @cf/openai/whisper)."""
    acc = str(cfg["params"].get("account_id") or "").strip()
    base = cfg["endpoint"].rstrip("/") or "https://api.cloudflare.com/client/v4"
    return f"{base}/accounts/{acc}/ai/run/{cfg['modele']}"


def _safe_header_value(v: str) -> str:
    """Strip CR/LF and double quotes so a controlled filename or mime cannot break
    out of its multipart header line and inject an extra part."""
    return v.replace("\r", " ").replace("\n", " ").replace('"', "").strip()


def _http_multipart(url: str, headers: dict[str, str], fields: dict[str, str],
                    file_field: str, filename: str, content: bytes, mime: str) -> Any:
    filename = _safe_header_value(filename) or "note.webm"
    mime = _safe_header_value(mime) or "application/octet-stream"
    boundary = "----adsum" + secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    data = b"".join(parts)
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (trusted provider URL)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise AIError(f"provider HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise AIError(f"provider unreachable: {exc}") from exc
    try:
        return _json.loads(raw) if raw else {}
    except ValueError as exc:
        raise AIError("provider returned invalid JSON") from exc


def load_active(capacite: str, role: str) -> dict[str, Any] | None:
    """The active provider row for a capability, with its key decrypted, or None."""
    row = db.fetch_one(
        "SELECT id, fournisseur, modele, endpoint, cle_chiffree, params "
        "FROM ai_provider_config WHERE capacite = %s AND actif LIMIT 1",
        (capacite,),
        role=role,
    )
    if not row:
        return None
    cle = ""
    if row["cle_chiffree"]:
        try:
            cle = crypto.decrypt_bytes(bytes(row["cle_chiffree"])).decode("utf-8")
        except ValueError as exc:  # key could not be decrypted (rotation gap)
            raise AIError("provider key could not be decrypted") from exc
    params = row["params"] if isinstance(row["params"], dict) else {}
    return {
        "fournisseur": row["fournisseur"],
        "modele": row["modele"],
        "endpoint": row["endpoint"] or "",
        "cle": cle,
        "params": params,
    }


_KEYLESS = frozenset({"selfhosted"})


def _ensure_usable(cfg: dict[str, Any], quoi: str) -> None:
    """A configured provider that needs a key but has none yields a clear, actionable
    error, so the moderator knows to add the key in the back office rather than seeing
    a raw provider HTTP code."""
    if cfg["fournisseur"] not in _KEYLESS and not cfg["cle"]:
        raise AIError(
            f"Le fournisseur {quoi} « {cfg['fournisseur']} » n'a pas de cle API. "
            "Ajoutez-la dans le back-office : Reglages IA (Fournisseurs IA)."
        )


def _auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    f = cfg["fournisseur"]
    key = cfg["cle"]
    if f == "azure_openai":
        return {"api-key": key} if key else {}
    if f == "google_gemini":
        return {"x-goog-api-key": key} if key else {}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _chat_url(cfg: dict[str, Any]) -> str:
    f = cfg["fournisseur"]
    ep = cfg["endpoint"].rstrip("/")
    if f == "azure_openai":
        dep = str(cfg["params"].get("deployment") or cfg["modele"])
        ver = str(cfg["params"].get("api_version") or "2024-06-01")
        return f"{ep}/openai/deployments/{dep}/chat/completions?api-version={ver}"
    if f == "google_gemini":
        base = ep or "https://generativelanguage.googleapis.com"
        return f"{base}/v1beta/models/{cfg['modele']}:generateContent"
    if f == "cloudflare":
        return _cf_url(cfg)
    if ep:
        return f"{ep}/chat/completions"
    return _CHAT_URL.get(f, "")


def _stt_url(cfg: dict[str, Any]) -> str:
    f = cfg["fournisseur"]
    ep = cfg["endpoint"].rstrip("/")
    if f == "azure_openai":
        dep = str(cfg["params"].get("deployment") or cfg["modele"])
        ver = str(cfg["params"].get("api_version") or "2024-06-01")
        return f"{ep}/openai/deployments/{dep}/audio/transcriptions?api-version={ver}"
    if f == "google_gemini":
        base = ep or "https://generativelanguage.googleapis.com"
        return f"{base}/v1beta/models/{cfg['modele']}:generateContent"
    if f == "cloudflare":
        return _cf_url(cfg)
    if ep:
        return f"{ep}/audio/transcriptions"
    return _STT_URL.get(f, "")


def _chat(cfg: dict[str, Any], system: str, user: str) -> str:
    """One chat completion, temperature 0 for a faithful, deterministic redaction."""
    url = _chat_url(cfg)
    if not url:
        raise AIError(f"no chat endpoint for provider {cfg['fournisseur']}")
    headers = _auth_headers(cfg)
    if cfg["fournisseur"] == "google_gemini":
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0},
        }
        data = _http_json(url, headers, body)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or [{}]
        return str(parts[0].get("text") or "").strip()
    if cfg["fournisseur"] == "cloudflare":
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0}
        data = _http_json(url, headers, body)
        return str((data.get("result") or {}).get("response") or "").strip()
    body = {
        "model": cfg["modele"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
    }
    data = _http_json(url, headers, body)
    choices = data.get("choices") or [{}]
    return str((choices[0].get("message") or {}).get("content") or "").strip()


def transcrire(content: bytes, filename: str, mime: str, role: str, language: str = "fr") -> str:
    """Verbatim speech-to-text with the active STT provider. Raises AIError when no
    provider is configured or the call fails."""
    cfg = load_active("stt", role)
    if not cfg:
        raise AIError("aucun fournisseur STT actif")
    _ensure_usable(cfg, "de transcription")
    url = _stt_url(cfg)
    if not url:
        raise AIError(f"no STT endpoint for provider {cfg['fournisseur']}")
    headers = _auth_headers(cfg)
    if cfg["fournisseur"] == "google_gemini":
        body = {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": mime or "audio/webm", "data": base64.b64encode(content).decode()}},
                {"text": "Transcris ce fichier audio mot pour mot, en francais, sans rien ajouter ni omettre."},
            ]}],
            "generationConfig": {"temperature": 0},
        }
        data = _http_json(url, headers, body)
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or [{}]
        return str(parts[0].get("text") or "").strip()
    if cfg["fournisseur"] == "cloudflare":
        # Cloudflare Workers AI Whisper takes the raw audio bytes as the POST body.
        data = _http_binary(url, headers, content, mime or "application/octet-stream")
        return str((data.get("result") or {}).get("text") or "").strip()
    fields = {"model": cfg["modele"], "language": language, "response_format": "json"}
    data = _http_multipart(url, headers, fields, "file", filename, content, mime or "audio/webm")
    return str(data.get("text") or "").strip()


_NUM_RE = re.compile(r"\d[\d .,]*\d|\d")


def verifier_fidelite(brut: str, redige: str) -> list[str]:
    """Deterministic guardrail on top of the prompt: every number/year present in
    the redaction must exist in the raw text (no invented figures), and none of the
    raw figures should silently vanish. Returns human-readable discrepancies."""
    def nums(s: str) -> set[str]:
        return {re.sub(r"[\s .,]", "", m) for m in _NUM_RE.findall(s) if re.sub(r"[\s .,]", "", m)}

    b, r = nums(brut), nums(redige)
    problemes: list[str] = []
    inventes = sorted(r - b)
    perdus = sorted(b - r)
    if inventes:
        problemes.append(f"Chiffres presents dans la redaction mais absents du brut: {', '.join(inventes)}")
    if perdus:
        problemes.append(f"Chiffres du brut absents de la redaction: {', '.join(perdus)}")
    if len(redige) > max(400, len(brut) * 2.2):
        problemes.append("La redaction est nettement plus longue que le brut (risque d'ajout).")
    return problemes


def ping_llm(cfg: dict[str, Any]) -> str:
    """A one-word round trip to verify an LLM provider config from the back office."""
    return _chat(cfg, "Reponds uniquement par: OK", "Dis OK")


def ping_stt(cfg: dict[str, Any]) -> str:
    """Auth/connectivity check for an STT provider WITHOUT an audio sample: a GET on
    the provider's models listing (OpenAI-compatible shape). A 200 proves the key and
    endpoint are valid. For a provider whose transcription URL is not OpenAI-shaped
    (e.g. Cloudflare, Gemini), we cannot probe without audio and say so honestly, so
    the status is never a false positive."""
    _ensure_usable(cfg, "de transcription")
    url = _stt_url(cfg)
    if not url:
        raise AIError(f"no stt endpoint for provider {cfg['fournisseur']}")
    if "/audio/transcriptions" not in url:
        raise AIError("test direct indisponible pour ce fournisseur : la clé est validée à l'usage réel du canal.")
    models_url = url.replace("/audio/transcriptions", "/models")
    data = _http_get_json(models_url, _auth_headers(cfg))
    n = len(data.get("data") or []) if isinstance(data, dict) else 0
    return f"Cle valide, {n} modele(s) accessibles." if n else "Cle acceptee par le fournisseur."


_CATEGORIES = frozenset({"projet", "activite", "organisation", "communication", "urgence", "autre"})
_PRIORITES = frozenset({"urgente", "haute", "normale", "basse"})

_ANALYSE_SYSTEM = (
    "Tu recois la transcription BRUTE et verbatim d'une note vocale: une demande d'instruction laissee "
    "par un moderateur qui n'a pas le temps de remplir un formulaire. Produis un objet JSON STRICT avec "
    "EXACTEMENT ces cles:\n"
    '- "titre": un titre court (4 a 8 mots), en francais, qui resume fidelement la demande. N\'invente RIEN.\n'
    '- "categorie": une seule valeur parmi: projet, activite, organisation, communication, urgence, autre.\n'
    '- "priorite": une seule valeur parmi: urgente, haute, normale, basse, selon l\'urgence exprimee.\n'
    '- "redige": la version REDIGEE professionnelle du texte, FIDELE a 100%. Regles absolues: n\'ajoute, ne '
    "retire, ne deforme AUCUN fait, nom, chiffre, date, montant ou decision. Corrige uniquement la grammaire, "
    "la structure (phrases, listes) et retire les hesitations orales. Conserve tous les nombres et noms exacts.\n"
    "Reponds UNIQUEMENT avec le JSON valide, sans texte autour, sans balises de code."
)


def _parse_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        obj = _json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                obj = _json.loads(m.group(0))
                return obj if isinstance(obj, dict) else {}
            except ValueError:
                return {}
        return {}


def analyser_note(brut: str, role: str) -> dict[str, Any]:
    """From a raw voice-note transcription, extract a title, a category and a priority
    AND produce the faithful professional redaction, in one LLM call. Lets the
    moderator drop only a voice note: the form fills itself, and stays editable.
    Returns {titre, categorie, priorite, redige, problemes}. Raises AIError when no
    LLM provider is active."""
    texte = (brut or "").strip()
    if not texte:
        return {"titre": "", "categorie": "autre", "priorite": "normale", "redige": "", "problemes": []}
    cfg = load_active("llm", role)
    if not cfg:
        raise AIError("aucun fournisseur LLM actif")
    _ensure_usable(cfg, "de redaction")
    data = _parse_json(_chat(cfg, _ANALYSE_SYSTEM, texte))
    cat = str(data.get("categorie") or "autre").strip().lower()
    prio = str(data.get("priorite") or "normale").strip().lower()
    redige = str(data.get("redige") or "").strip()
    return {
        "titre": str(data.get("titre") or "").strip()[:200],
        "categorie": cat if cat in _CATEGORIES else "autre",
        "priorite": prio if prio in _PRIORITES else "normale",
        "redige": redige,
        "problemes": verifier_fidelite(texte, redige) if redige else [],
    }


def rediger(brut: str, role: str) -> tuple[str, list[str]]:
    """Professional redaction of a raw transcription with the active LLM provider,
    plus a fidelity check. Returns (redaction, discrepancies)."""
    texte = (brut or "").strip()
    if not texte:
        return "", []
    cfg = load_active("llm", role)
    if not cfg:
        raise AIError("aucun fournisseur LLM actif")
    _ensure_usable(cfg, "de redaction")
    redige = _chat(cfg, _REDACTION_SYSTEM, texte)
    if not redige:
        raise AIError("redaction vide renvoyee par le fournisseur")
    return redige, verifier_fidelite(texte, redige)
