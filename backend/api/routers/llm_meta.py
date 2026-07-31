"""LLM provider metadata — keeps the frontend's provider/model pickers in sync.

``GET /api/llm/providers`` is the single source of truth for which providers the
backend will accept, what each one's default models are, and whether the server
already holds a key for it (so the UI can show "server fallback available"
instead of demanding one).

``POST /api/llm/models`` asks a provider for its live catalogue using the key the
user just pasted, so the model dropdown reflects what the account can actually
call rather than a hard-coded guess. Keys are used for that one request and
never stored.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import llm_providers

from ..ai import anthropic_adapter

router = APIRouter()
logger = logging.getLogger("mzqa.ai.llm_meta")


class ProviderInfo(BaseModel):
    id: str
    label: str
    dialect: str
    base_url: str
    chat_model: str
    reasoner_model: str
    models: list[str] = Field(default_factory=list)
    key_placeholder: str
    docs_url: str
    env_names: list[str] = Field(default_factory=list)
    has_server_key: bool
    # Which env var supplied the server-side key, and whether it came from the
    # process environment or straight out of HKCU\Environment. The key value
    # itself is never returned.
    server_key_env: str | None = None
    server_key_origin: str | None = None


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    default_provider: str


class ModelsRequest(BaseModel):
    provider: str
    api_key: str | None = None


class ModelsResponse(BaseModel):
    provider: str
    models: list[str]
    source: str            # "provider" (live catalogue) | "registry" (static hints)
    warning: str | None = None


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    return ProvidersResponse(
        providers=[ProviderInfo(**row) for row in llm_providers.describe()],
        default_provider=llm_providers.default_provider_id(),
    )


@router.post("/models", response_model=ModelsResponse)
async def list_models(req: ModelsRequest) -> ModelsResponse:
    """Live model catalogue for one provider. Falls back to the registry hints."""
    try:
        prov = llm_providers.get(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_key = (req.api_key or "").strip() or llm_providers.resolve_env_key(prov.id)
    if not api_key:
        return ModelsResponse(
            provider=prov.id, models=list(prov.models()), source="registry",
            warning=f"No {prov.label} key available — showing the built-in defaults.",
        )

    try:
        if prov.dialect == "anthropic":
            models = await anthropic_adapter.list_models(
                api_key=api_key, base_url=llm_providers.resolve_base_url(prov.id),
            )
        else:
            models = await _openai_dialect_models(prov, api_key)
    except Exception as exc:  # noqa: BLE001 - the dropdown must never hard-fail
        logger.warning("model listing failed for %s: %s", prov.id, exc)
        detail = str(exc).replace(api_key, "***")[:200]
        return ModelsResponse(
            provider=prov.id, models=list(prov.models()), source="registry",
            warning=f"Could not read the {prov.label} catalogue ({detail}); showing defaults.",
        )

    merged = list(dict.fromkeys([*prov.models(), *sorted(models)]))
    return ModelsResponse(provider=prov.id, models=merged, source="provider")


async def _openai_dialect_models(prov: llm_providers.Provider, api_key: str) -> list[str]:
    url = f"{llm_providers.resolve_base_url(prov.id)}/models"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    data = r.json().get("data") or []
    # Gemini's OpenAI-compat layer prefixes ids with "models/"; strip it so the id
    # can be sent straight back as the `model` field.
    return [str(m.get("id") or "").removeprefix("models/") for m in data if m.get("id")]
