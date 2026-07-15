# ruff: noqa: E501 - long provider guide strings (URLs, French instructions) are kept readable
"""Back-office management of the AI providers (speech-to-text and redaction).

Providers are created, keyed, switched and tested here, so the moderator channel
can change model or vendor in one click without a redeploy. The API key is
accepted in clear on write, stored Fernet-encrypted, and NEVER returned: reads
expose only whether a key is present. Guarded by ``integrations.administrer``.
"""
from __future__ import annotations

import json as _json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import ai_providers, audit, crypto, db
from .fields import LineStr, ShortStr
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/ai", tags=["ai-config"])

_CAPACITES = ("stt", "llm")
_FOURNISSEURS = (
    "openai", "groq", "perplexity", "azure_openai", "google_gemini", "cloudflare", "selfhosted", "anthropic"
)


class ProviderIn(BaseModel):
    capacite: ShortStr
    fournisseur: ShortStr
    libelle: LineStr
    modele: LineStr
    endpoint: str | None = None
    cle: str | None = None
    params: dict[str, Any] = {}
    gratuit: bool = False
    ordre: int = 0


class ProviderPatch(BaseModel):
    libelle: LineStr | None = None
    modele: LineStr | None = None
    endpoint: str | None = None
    cle: str | None = None  # new key; empty string clears it, None leaves it unchanged
    params: dict[str, Any] | None = None
    gratuit: bool | None = None
    ordre: int | None = None


class ProviderOut(BaseModel):
    id: str
    capacite: str
    fournisseur: str
    libelle: str
    modele: str
    endpoint: str | None = None
    cle_presente: bool
    params: dict[str, Any]
    actif: bool
    gratuit: bool
    ordre: int


def _out(row: dict[str, Any]) -> ProviderOut:
    return ProviderOut(
        id=str(row["id"]),
        capacite=row["capacite"],
        fournisseur=row["fournisseur"],
        libelle=row["libelle"],
        modele=row["modele"],
        endpoint=row["endpoint"],
        cle_presente=row["cle_chiffree"] is not None,
        params=row["params"] if isinstance(row["params"], dict) else {},
        actif=bool(row["actif"]),
        gratuit=bool(row["gratuit"]),
        ordre=int(row["ordre"]),
    )


_COLS = "id, capacite, fournisseur, libelle, modele, endpoint, cle_chiffree, params, actif, gratuit, ordre"


# Per-provider onboarding guide shown in the back-office so an administrator knows,
# for each provider, the official site, where to create an account, the exact page
# that issues the API key (not a cryptocurrency token: a plain API access key), the
# free-tier situation and any extra field the provider needs. Static, no secret.
_GUIDES: dict[str, dict[str, Any]] = {
    "groq": {
        "libelle": "Groq",
        "gratuit": True,
        "resume": "Le plus simple et gratuit pour commencer. Transcription (Whisper) et redaction (Llama 3.3) tres rapides.",
        "site": "https://groq.com",
        "compte_url": "https://console.groq.com",
        "cle_url": "https://console.groq.com/keys",
        "cle_libelle": "Cle API (commence par gsk_)",
        "etapes": [
            "Ouvrez https://console.groq.com et creez un compte gratuit (Google ou e-mail).",
            "Allez dans API Keys (https://console.groq.com/keys), cliquez sur Create API Key.",
            "Copiez la cle (elle commence par gsk_) et collez-la dans le champ Cle API ci-dessous.",
            "Modele suggere: whisper-large-v3 (transcription) ou llama-3.3-70b-versatile (redaction).",
        ],
        "params": [],
    },
    "cloudflare": {
        "libelle": "Cloudflare Workers AI",
        "gratuit": True,
        "resume": "Gratuit (quota quotidien genereux). Demande DEUX informations: l'Account ID et un jeton API.",
        "site": "https://www.cloudflare.com/developer-platform/products/workers-ai/",
        "compte_url": "https://dash.cloudflare.com/sign-up",
        "cle_url": "https://dash.cloudflare.com/profile/api-tokens",
        "cle_libelle": "Jeton API (API Token) avec la permission Workers AI",
        "etapes": [
            "Creez un compte sur https://dash.cloudflare.com/sign-up.",
            "Recuperez votre Account ID: sur https://dash.cloudflare.com, il est affiche dans l'URL et dans le panneau du compte. Reportez-le dans le parametre account_id ci-dessous.",
            "Creez le jeton: https://dash.cloudflare.com/profile/api-tokens > Create Token > modele 'Workers AI' (permission Account > Workers AI > Read/Edit).",
            "Collez ce jeton dans Cle API. Un 401 au test signifie un jeton invalide ou une permission Workers AI manquante.",
            "Modeles: @cf/openai/whisper (transcription), @cf/meta/llama-3.3-70b-instruct-fp8-fast (redaction).",
        ],
        "params": ["account_id"],
    },
    "google_gemini": {
        "libelle": "Google Gemini (AI Studio)",
        "gratuit": True,
        "resume": "Tier gratuit chez Google AI Studio. Cle obtenue en un clic.",
        "site": "https://ai.google.dev",
        "compte_url": "https://aistudio.google.com",
        "cle_url": "https://aistudio.google.com/apikey",
        "cle_libelle": "Cle API Google AI Studio",
        "etapes": [
            "Ouvrez https://aistudio.google.com et connectez-vous avec un compte Google.",
            "Allez sur https://aistudio.google.com/apikey puis Create API key.",
            "Copiez la cle et collez-la dans Cle API. Modele suggere: gemini-2.0-flash.",
        ],
        "params": [],
    },
    "openai": {
        "libelle": "OpenAI",
        "gratuit": False,
        "resume": "Payant (credit prepaye). Haute qualite. Cle API classique.",
        "site": "https://openai.com",
        "compte_url": "https://platform.openai.com/signup",
        "cle_url": "https://platform.openai.com/api-keys",
        "cle_libelle": "Cle API (commence par sk-)",
        "etapes": [
            "Creez un compte sur https://platform.openai.com et ajoutez un moyen de paiement (service payant).",
            "Allez sur https://platform.openai.com/api-keys > Create new secret key.",
            "Copiez la cle (sk-...) dans Cle API. Modeles: gpt-4o-transcribe / whisper-1 (STT), gpt-4o-mini (redaction).",
        ],
        "params": [],
    },
    "perplexity": {
        "libelle": "Perplexity",
        "gratuit": False,
        "resume": "Payant. Bonne redaction (modeles Sonar). Pas de transcription audio.",
        "site": "https://www.perplexity.ai",
        "compte_url": "https://www.perplexity.ai",
        "cle_url": "https://www.perplexity.ai/settings/api",
        "cle_libelle": "Cle API Perplexity (commence par pplx-)",
        "etapes": [
            "Connectez-vous sur https://www.perplexity.ai puis ouvrez Settings > API.",
            "Ajoutez un moyen de paiement puis generez une cle (pplx-...).",
            "Collez-la dans Cle API. Modele suggere: sonar. Uniquement pour la redaction (LLM), pas la transcription.",
        ],
        "params": [],
    },
    "azure_openai": {
        "libelle": "Azure OpenAI",
        "gratuit": False,
        "resume": "Payant, entreprise, heberge en UE (utile RGPD). Demande endpoint + nom du deploiement.",
        "site": "https://azure.microsoft.com/products/ai-services/openai-service",
        "compte_url": "https://portal.azure.com",
        "cle_url": "https://portal.azure.com",
        "cle_libelle": "Cle 1/2 de la ressource Azure OpenAI (Keys and Endpoint)",
        "etapes": [
            "Dans https://portal.azure.com, creez une ressource Azure OpenAI et un deploiement de modele.",
            "Dans la ressource > Keys and Endpoint: copiez la Cle et l'Endpoint.",
            "Renseignez Cle API, le parametre endpoint (URL de la ressource), deployment (nom du deploiement) et api_version.",
        ],
        "params": ["endpoint", "deployment", "api_version"],
    },
    "selfhosted": {
        "libelle": "Auto-heberge (0 tiers)",
        "gratuit": True,
        "resume": "Votre propre serveur (faster-whisper / Ollama / vLLM). Aucune donnee ne sort de votre infrastructure.",
        "site": "https://github.com/ggml-org/whisper.cpp",
        "compte_url": "",
        "cle_url": "",
        "cle_libelle": "Cle API si votre serveur en exige une (sinon laisser vide)",
        "etapes": [
            "Deployez un service compatible OpenAI (ex. faster-whisper-server pour la transcription, Ollama/vLLM pour la redaction).",
            "Renseignez le parametre endpoint avec l'URL de votre serveur.",
            "Ajoutez une Cle API seulement si votre serveur en demande une.",
        ],
        "params": ["endpoint"],
    },
}


@router.get("/catalogue")
def catalogue(
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> dict[str, Any]:
    """Known providers and suggested models, to guide the back-office form. Static,
    no secret. STT and LLM are distinct capabilities (Perplexity has no STT)."""
    return {
        "capacites": list(_CAPACITES),
        "fournisseurs": list(_FOURNISSEURS),
        "guides": _GUIDES,
        "suggestions": {
            "stt": [
                {"fournisseur": "groq", "modele": "whisper-large-v3", "gratuit": True, "note": "Rapide, tier gratuit"},
                {"fournisseur": "cloudflare", "modele": "@cf/openai/whisper", "gratuit": True,
                 "note": "Gratuit, compte Cloudflare (account_id dans params)"},
                {"fournisseur": "openai", "modele": "gpt-4o-transcribe", "note": "Haute fidelite FR"},
                {"fournisseur": "openai", "modele": "whisper-1", "note": "Whisper hebergé OpenAI"},
                {"fournisseur": "google_gemini", "modele": "gemini-2.0-flash", "gratuit": True, "note": "Tier gratuit"},
                {"fournisseur": "azure_openai", "modele": "whisper", "note": "Heberge UE (RGPD)"},
                {"fournisseur": "selfhosted", "modele": "whisper-large-v3", "note": "Auto-heberge (0 tiers)"},
            ],
            "llm": [
                {"fournisseur": "perplexity", "modele": "sonar", "note": "Defaut redaction pro"},
                {"fournisseur": "groq", "modele": "llama-3.3-70b-versatile", "gratuit": True, "note": "Gratuit"},
                {"fournisseur": "cloudflare", "modele": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "gratuit": True,
                 "note": "Gratuit, compte Cloudflare (account_id dans params)"},
                {"fournisseur": "openai", "modele": "gpt-4o-mini", "note": "Economique"},
                {"fournisseur": "google_gemini", "modele": "gemini-2.0-flash", "gratuit": True, "note": "Tier gratuit"},
                {"fournisseur": "azure_openai", "modele": "gpt-4o-mini", "note": "Heberge UE (RGPD)"},
                {"fournisseur": "selfhosted", "modele": "llama-3.3-70b", "note": "Auto-heberge"},
            ],
        },
    }


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> list[ProviderOut]:
    rows = db.fetch_all(
        f"SELECT {_COLS} FROM ai_provider_config ORDER BY capacite, ordre, libelle", (), role=user.role
    )
    return [_out(r) for r in rows]


def _valider(capacite: str, fournisseur: str) -> None:
    if capacite not in _CAPACITES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="capacite invalide (stt ou llm)")
    if fournisseur not in _FOURNISSEURS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fournisseur inconnu")


def _valider_endpoint(endpoint: str | None) -> None:
    """Only http(s) endpoints are accepted, so a provider row can never point the
    server at file://, gopher:// or another local scheme (defence in depth)."""
    if endpoint and not endpoint.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endpoint doit commencer par http:// ou https://")


@router.post("/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(
    payload: ProviderIn,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> ProviderOut:
    _valider(payload.capacite, payload.fournisseur)
    _valider_endpoint(payload.endpoint)
    cle_chiffree = crypto.encrypt_bytes(payload.cle.encode()) if payload.cle else None
    created = db.execute(
        "INSERT INTO ai_provider_config "
        "(capacite, fournisseur, libelle, modele, endpoint, cle_chiffree, params, gratuit, ordre, cree_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) RETURNING " + _COLS,
        (
            payload.capacite, payload.fournisseur, payload.libelle, payload.modele, payload.endpoint or None,
            cle_chiffree, _json.dumps(payload.params), payload.gratuit, payload.ordre, user.id,
        ),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="provider not created")
    audit.log(user.id, user.role, "creation_ai_provider", "ai_provider_config", str(created["id"]),
              {"capacite": payload.capacite, "fournisseur": payload.fournisseur, "modele": payload.modele})
    return _out(created)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: str,
    payload: ProviderPatch,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> ProviderOut:
    fields = payload.model_dump(exclude_unset=True)
    if "endpoint" in fields:
        _valider_endpoint(fields["endpoint"])
    sets: list[str] = []
    values: list[Any] = []
    if "cle" in fields:
        cle = fields.pop("cle")
        sets.append("cle_chiffree = %s")
        values.append(crypto.encrypt_bytes(cle.encode()) if cle else None)
    if "params" in fields:
        sets.append("params = %s::jsonb")
        values.append(_json.dumps(fields.pop("params") or {}))
    for key in ("libelle", "modele", "endpoint", "gratuit", "ordre"):
        if key in fields:
            sets.append(f"{key} = %s")
            values.append(fields[key])
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="rien a modifier")
    updated = db.execute(
        f"UPDATE ai_provider_config SET {', '.join(sets)}, maj_le = now() WHERE id = %s RETURNING " + _COLS,
        (*values, provider_id),
        role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
    audit.log(user.id, user.role, "modification_ai_provider", "ai_provider_config", provider_id,
              {"champs": sorted(k for k in ("cle", "params", "libelle", "modele", "endpoint", "gratuit", "ordre")
                                if k in payload.model_dump(exclude_unset=True))})
    return _out(updated)


@router.post("/providers/{provider_id}/activer", response_model=ProviderOut)
def activer_provider(
    provider_id: str,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> ProviderOut:
    """Make this provider the active one for its capability (one click switch). The
    partial unique index allows a single active row per capability, so we clear the
    others first, atomically."""
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        cur.execute("SELECT capacite FROM ai_provider_config WHERE id = %s", (provider_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
        cur.execute("UPDATE ai_provider_config SET actif = false WHERE capacite = %s", (row["capacite"],))
        cur.execute(
            f"UPDATE ai_provider_config SET actif = true, maj_le = now() WHERE id = %s RETURNING {_COLS}",
            (provider_id,),
        )
        updated = cur.fetchone()
    audit.log(user.id, user.role, "activation_ai_provider", "ai_provider_config", provider_id,
              {"capacite": row["capacite"]})
    return _out(updated)


@router.post("/providers/{provider_id}/desactiver", response_model=ProviderOut)
def desactiver_provider(
    provider_id: str,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> ProviderOut:
    """Turn a provider off for its capability without deleting it (dissociate a
    capacity). The channel then reports 'no active provider' for that capability until
    another is activated, instead of a provider being silently removed."""
    updated = db.execute(
        f"UPDATE ai_provider_config SET actif = false, maj_le = now() WHERE id = %s RETURNING {_COLS}",
        (provider_id,), role=user.role,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
    audit.log(user.id, user.role, "desactivation_ai_provider", "ai_provider_config", provider_id, {})
    return _out(updated)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: str,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> None:
    row = db.fetch_one(
        "SELECT actif, capacite FROM ai_provider_config WHERE id = %s", (provider_id,), role=user.role
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
    # Guard: never let the ACTIVE provider be removed silently, which would stop the
    # channel's transcription/redaction. The operator must activate a replacement (or
    # explicitly deactivate) first.
    if bool(row["actif"]):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Ce fournisseur est actif : le supprimer couperait "
                + ("la transcription" if row["capacite"] == "stt" else "la rédaction")
                + " du canal. Activez d'abord un autre fournisseur, ou désactivez celui-ci."
            ),
        )
    db.execute("DELETE FROM ai_provider_config WHERE id = %s", (provider_id,), role=user.role)
    audit.log(user.id, user.role, "suppression_ai_provider", "ai_provider_config", provider_id, {})


class TestResult(BaseModel):
    ok: bool
    detail: str


@router.post("/providers/{provider_id}/test", response_model=TestResult)
def tester_provider(
    provider_id: str,
    user: Annotated[UserMe, Depends(require_permission("integrations.administrer"))],
) -> TestResult:
    """Live connectivity check for an LLM provider (a one-word round trip). STT is
    not tested here (it needs an audio sample); the channel exercises it end to end."""
    row = db.fetch_one(
        "SELECT capacite, fournisseur, modele, endpoint, cle_chiffree, params "
        "FROM ai_provider_config WHERE id = %s",
        (provider_id,),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider not found")
    cle = ""
    if row["cle_chiffree"]:
        try:
            cle = crypto.decrypt_bytes(bytes(row["cle_chiffree"])).decode("utf-8")
        except ValueError:
            return TestResult(ok=False, detail="Cle indechiffrable.")
    cfg = {
        "fournisseur": row["fournisseur"], "modele": row["modele"], "endpoint": row["endpoint"] or "",
        "cle": cle, "params": row["params"] if isinstance(row["params"], dict) else {},
    }
    try:
        # STT is probed with a keyed models listing (no audio sample needed); LLM does
        # a one-word round trip. Neither returns a false positive.
        rep = ai_providers.ping_stt(cfg) if row["capacite"] == "stt" else ai_providers.ping_llm(cfg)
        return TestResult(ok=bool(rep), detail=(rep[:200] or "reponse vide"))
    except ai_providers.AIError as exc:
        return TestResult(ok=False, detail=str(exc)[:200])
