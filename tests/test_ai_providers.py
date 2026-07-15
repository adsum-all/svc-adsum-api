"""Unit tests for the pluggable AI provider layer.

Proves the multi-provider wiring is correct without any real key: URL and header
construction per provider, the anti-hallucination fidelity guardrail, and the
transcribe/redact flows with the HTTP layer mocked.
"""
from __future__ import annotations

import pytest

from app import ai_providers as ai


def cfg(**over):
    base = {"fournisseur": "openai", "modele": "gpt-4o-mini", "endpoint": "", "cle": "SECRET", "params": {}}
    base.update(over)
    return base


# ---- URL construction per provider ----

def test_chat_url_openai_compatibles():
    assert ai._chat_url(cfg(fournisseur="openai")) == "https://api.openai.com/v1/chat/completions"
    assert ai._chat_url(cfg(fournisseur="groq")) == "https://api.groq.com/openai/v1/chat/completions"
    assert ai._chat_url(cfg(fournisseur="perplexity")) == "https://api.perplexity.ai/chat/completions"


def test_chat_url_selfhosted_uses_endpoint():
    assert ai._chat_url(cfg(fournisseur="selfhosted", endpoint="https://vm.local/v1")) == \
        "https://vm.local/v1/chat/completions"


def test_chat_url_azure_builds_deployment_path():
    c = cfg(fournisseur="azure_openai", endpoint="https://r.openai.azure.com",
            params={"deployment": "gpt4o", "api_version": "2024-06-01"})
    assert ai._chat_url(c) == \
        "https://r.openai.azure.com/openai/deployments/gpt4o/chat/completions?api-version=2024-06-01"


def test_chat_url_gemini():
    c = cfg(fournisseur="google_gemini", modele="gemini-2.0-flash")
    assert ai._chat_url(c) == \
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


def test_cloudflare_url_and_shapes(monkeypatch):
    c = cfg(fournisseur="cloudflare", modele="@cf/openai/whisper", params={"account_id": "ACC123"})
    assert ai._stt_url(c) == "https://api.cloudflare.com/client/v4/accounts/ACC123/ai/run/@cf/openai/whisper"
    llm = cfg(fournisseur="cloudflare", modele="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
              params={"account_id": "ACC123"})
    assert ai._chat_url(llm).endswith("/accounts/ACC123/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast")

    monkeypatch.setattr(ai, "load_active", lambda cap, role: c)
    monkeypatch.setattr(ai, "_http_binary", lambda url, headers, content, ct: {"result": {"text": "bonjour"}})
    assert ai.transcrire(b"audio", "n.webm", "audio/webm", "super_admin") == "bonjour"

    monkeypatch.setattr(ai, "load_active", lambda cap, role: llm)
    monkeypatch.setattr(ai, "_http_json", lambda url, headers, body: {"result": {"response": "Redige."}})
    redige, _ = ai.rediger("texte", "super_admin")
    assert redige == "Redige."


def test_stt_url_per_provider():
    assert ai._stt_url(cfg(fournisseur="openai")) == "https://api.openai.com/v1/audio/transcriptions"
    assert ai._stt_url(cfg(fournisseur="groq")) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert ai._stt_url(cfg(fournisseur="selfhosted", endpoint="https://vm/v1")) == \
        "https://vm/v1/audio/transcriptions"


def test_auth_headers_per_provider():
    assert ai._auth_headers(cfg(fournisseur="openai")) == {"Authorization": "Bearer SECRET"}
    assert ai._auth_headers(cfg(fournisseur="azure_openai")) == {"api-key": "SECRET"}
    assert ai._auth_headers(cfg(fournisseur="google_gemini")) == {"x-goog-api-key": "SECRET"}
    assert ai._auth_headers(cfg(cle="")) == {}


# ---- Anti-hallucination fidelity guardrail ----

def test_fidelite_ok_when_numbers_match():
    assert ai.verifier_fidelite("Budget de 1500 euros pour 3 lots.", "Budget : 1 500 euros pour 3 lots.") == []


def test_fidelite_flags_invented_number():
    problemes = ai.verifier_fidelite("Prevoir 3 lots.", "Prevoir 3 lots pour 2027 avec 5 equipes.")
    assert any("absents du brut" in p for p in problemes)


def test_fidelite_flags_dropped_number():
    problemes = ai.verifier_fidelite("Rendez-vous le 12 a 14h avec 8 personnes.", "Rendez-vous prevu avec l'equipe.")
    assert any("absents de la redaction" in p for p in problemes)


# ---- transcribe / redact flows with HTTP mocked ----

def test_transcrire_openai_compatible(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: cfg(fournisseur="groq", modele="whisper-large-v3"))
    captured = {}

    def fake_multipart(url, headers, fields, file_field, filename, content, mime):
        captured.update(url=url, headers=headers, fields=fields, filename=filename)
        return {"text": "Bonjour ceci est une note vocale."}

    monkeypatch.setattr(ai, "_http_multipart", fake_multipart)
    txt = ai.transcrire(b"audiobytes", "note.webm", "audio/webm", "super_admin")
    assert txt == "Bonjour ceci est une note vocale."
    assert captured["url"].endswith("/audio/transcriptions")
    assert captured["fields"]["model"] == "whisper-large-v3"
    assert captured["headers"] == {"Authorization": "Bearer SECRET"}


def test_transcrire_no_provider_raises(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: None)
    with pytest.raises(ai.AIError):
        ai.transcrire(b"x", "n.webm", "audio/webm", "super_admin")


def test_rediger_returns_text_and_fidelity(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: cfg(fournisseur="perplexity", modele="sonar"))

    def fake_json(url, headers, body):
        # OpenAI-compatible chat response shape
        assert body["temperature"] == 0
        assert body["messages"][0]["role"] == "system"
        return {"choices": [{"message": {"content": "Budget : 1 500 euros pour 3 lots."}}]}

    monkeypatch.setattr(ai, "_http_json", fake_json)
    redige, problemes = ai.rediger("budget de 1500 euros pour 3 lots", "super_admin")
    assert "1 500" in redige
    assert problemes == []  # 1500 and 3 preserved


def test_rediger_gemini_shape(monkeypatch):
    gcfg = cfg(fournisseur="google_gemini", modele="gemini-2.0-flash")
    monkeypatch.setattr(ai, "load_active", lambda cap, role: gcfg)

    def fake_json(url, headers, body):
        assert "generateContent" in url
        assert headers == {"x-goog-api-key": "SECRET"}
        return {"candidates": [{"content": {"parts": [{"text": "Texte redige."}]}}]}

    monkeypatch.setattr(ai, "_http_json", fake_json)
    redige, _ = ai.rediger("texte brut", "super_admin")
    assert redige == "Texte redige."


def test_rediger_empty_brut_returns_empty(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: cfg())
    assert ai.rediger("   ", "super_admin") == ("", [])


# ---- analyser_note: voice-note auto-fill ----

def test_parse_json_strips_code_fences():
    assert ai._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai._parse_json('bla {"a": 2} bla') == {"a": 2}
    assert ai._parse_json("pas du json") == {}


def test_analyser_note_extracts_fields_and_redaction(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: cfg(fournisseur="perplexity", modele="sonar"))

    def fake_json(url, headers, body):
        return {"choices": [{"message": {"content": _json_reply()}}]}

    monkeypatch.setattr(ai, "_http_json", fake_json)
    out = ai.analyser_note("faut organiser le cahier des charges pour 3 lots avant le 12", "super_admin")
    assert out["titre"] == "Organiser le cahier des charges"
    assert out["categorie"] == "projet"
    assert out["priorite"] == "haute"
    assert "3 lots" in out["redige"]
    assert out["problemes"] == []  # 3 and 12 preserved


def test_analyser_note_sanitises_invalid_enums(monkeypatch):
    monkeypatch.setattr(ai, "load_active", lambda cap, role: cfg())

    bad = '{"titre":"X","categorie":"n_importe_quoi","priorite":"ultra","redige":"X"}'

    def fake_json(url, headers, body):
        return {"choices": [{"message": {"content": bad}}]}

    monkeypatch.setattr(ai, "_http_json", fake_json)
    out = ai.analyser_note("texte", "super_admin")
    assert out["categorie"] == "autre"   # invalid -> fallback
    assert out["priorite"] == "normale"  # invalid -> fallback


def _json_reply() -> str:
    import json
    return json.dumps({
        "titre": "Organiser le cahier des charges",
        "categorie": "projet",
        "priorite": "haute",
        "redige": "Organiser le cahier des charges pour 3 lots avant le 12.",
    })
