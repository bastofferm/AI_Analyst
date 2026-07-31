"""DeepSeek/OpenAI-compatible client factory for mapping review workflows.

The concept-mapping reassessment path is DeepSeek-first by design.  This module
intentionally avoids the ``openai`` Python package and talks directly to
OpenAI-compatible HTTP APIs.
"""
from __future__ import annotations

import os
import json as json_lib
import site
from types import SimpleNamespace
from typing import Any
import urllib.error
import urllib.request

site.addsitedir(site.getusersitepackages())


def _coalesce_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                registry_value, _ = winreg.QueryValueEx(key, name)
                if registry_value:
                    return str(registry_value)
        except Exception:
            pass
    return ""


def _require(value: str, message: str) -> str:
    if not value:
        raise RuntimeError(message)
    return value


class _HTTPJSONResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"LLM API returned HTTP {self.status_code}: {self._payload}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _LazyHTTPClient:
    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> _HTTPJSONResponse:
        body = json_lib.dumps(json, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120.0) as response:
                raw = response.read().decode("utf-8")
                payload = json_lib.loads(raw) if raw else {}
                return _HTTPJSONResponse(response.status, payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json_lib.loads(raw) if raw else {}
            except Exception:
                payload = {"error": raw}
            return _HTTPJSONResponse(exc.code, payload)


class _ChatCompletionsAPI:
    def __init__(self, client: _LazyHTTPClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def create(self, **payload: Any) -> SimpleNamespace:
        response = self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        choices = []
        for item in data.get("choices", []):
            message = item.get("message") or {}
            choices.append(SimpleNamespace(message=SimpleNamespace(content=message.get("content"))))
        return SimpleNamespace(choices=choices, raw=data)


class _EmbeddingsAPI:
    def __init__(self, client: _LazyHTTPClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def create(self, **payload: Any) -> SimpleNamespace:
        response = self._client.post(
            f"{self._base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        rows = []
        for item in data.get("data", []):
            rows.append(SimpleNamespace(embedding=item.get("embedding"), index=item.get("index")))
        return SimpleNamespace(data=rows, raw=data)


class _OpenAICompatibleClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        self._http = _LazyHTTPClient()
        self.chat = SimpleNamespace(completions=_ChatCompletionsAPI(self._http, base_url, api_key))
        self.embeddings = _EmbeddingsAPI(self._http, base_url, api_key)


def get_embedding_client():
    """Return a configured embedding API client."""
    api_key = _require(
        _coalesce_env("EMBEDDING_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
        "Embedding API key missing. Set EMBEDDING_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY.",
    )
    base_url = _coalesce_env("EMBEDDING_BASE_URL", "LLM_BASE_URL") or "https://api.deepseek.com/v1"
    return _OpenAICompatibleClient(api_key=api_key, base_url=base_url)


def get_llm_client():
    """Return a configured DeepSeek LLM client for reasoning tasks."""
    api_key = _require(
        _coalesce_env("DEEPSEEK_API_KEY"),
        "DeepSeek LLM API key missing. Set DEEPSEEK_API_KEY.",
    )
    base_url = _coalesce_env("LLM_BASE_URL") or "https://api.deepseek.com/v1"
    return _OpenAICompatibleClient(api_key=api_key, base_url=base_url)


def get_llm_model(default: str = "deepseek-chat") -> str:
    """Return the DeepSeek chat model used by mapping reassessment."""
    return _coalesce_env("LLM_MODEL", "DEEPSEEK_MODEL") or default
