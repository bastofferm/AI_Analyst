"""ChatOpenAICompat — LangChain BaseChatModel-Wrapper für OpenAI-Dialekt-Provider.

Deckt DeepSeek, OpenAI, Moonshot und Gemini ab (alle sprechen
``POST {base_url}/chat/completions`); Provider-Metadaten kommen aus der
Registry ``backend/llm_providers.py`` (Host-Whitelist, Caps, Env-Key-Namen).
``ChatDeepSeek`` bleibt als Alias erhalten, damit bestehende Importe weiter
funktionieren. Claude läuft NICHT hierüber — dafür gibt es ``ChatAnthropic``
über ``xbrl_sec.llm.factory``.

DeepSeek ignoriert `tool_choice="required"` gelegentlich — der Wrapper
validiert das Response-Format und re-prompted bei Verletzung (max 2 Retries),
failt dann hart.

Bewusst kein langchain-openai-Adapter, weil DeepSeek leicht abweichende
tool-Semantik hat und wir die Per-Request-API-Key-Logik aus dem Dashboard
behalten wollen.
"""
from __future__ import annotations

import json
from typing import Any, Iterator, List, Mapping, Optional, Sequence, Type

import httpx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ChatMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, PrivateAttr, model_validator

import llm_providers

DEFAULT_PROVIDER = "deepseek"
DEFAULT_BASE_URL = llm_providers.PROVIDERS["deepseek"].base_url
DEFAULT_CHAT_MODEL = llm_providers.PROVIDERS["deepseek"].chat_model
ALLOWED_DEEPSEEK_HOSTS = set(llm_providers.PROVIDERS["deepseek"].hosts)


class DeepSeekError(RuntimeError):
    """Raised for any provider HTTP or protocol violation.

    Name kept for backwards compatibility with existing ``except`` clauses;
    ``LLMWireError`` is the provider-neutral alias.
    """


LLMWireError = DeepSeekError


def resolve_env_key(provider: str | None = None) -> str:
    """Find an API key in env or the Windows user registry for ``provider``."""
    return llm_providers.resolve_env_key(provider or DEFAULT_PROVIDER)


def _normalize_tool_choice(tool_choice: str | dict[str, Any]) -> str | dict[str, Any]:
    """DeepSeek's thinking-mode models reject `tool_choice="required"` (and any
    specific-tool object) outright with a 400 ("Thinking mode does not support
    this tool_choice") — only `none`/`auto` are accepted on the wire. LangChain's
    default `with_structured_output` binds tools with `tool_choice="any"` (its
    provider-agnostic "force some tool" alias), which hits the same rejection.
    We map every forcing request down to `auto` on the wire and rely on
    `_force_tool_use`/`_enforce_tool_choice` (which re-prompts in plain text
    when no tool call comes back) to actually guarantee a tool call.
    """
    if tool_choice == "none":
        return "none"
    return "auto"


def _validate_request(provider: str, base_url: str, model: str) -> None:
    """Pin the request to the provider's host allowlist (models stay free-form)."""
    try:
        llm_providers.validate(provider, base_url, model)
    except llm_providers.LLMProviderError as exc:
        raise DeepSeekError(str(exc)) from exc


def _message_to_openai(msg: BaseMessage) -> dict[str, Any]:
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content}
    if isinstance(msg, HumanMessage):
        return {"role": "user", "content": msg.content}
    if isinstance(msg, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id,
            "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
        }
    if isinstance(msg, AIMessage):
        out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("args") or {}),
                    },
                }
                for i, tc in enumerate(msg.tool_calls)
            ]
        return out
    if isinstance(msg, ChatMessage):
        return {"role": msg.role, "content": msg.content}
    return {"role": "user", "content": str(msg.content)}


def _parse_tool_calls(raw_tool_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
    if not raw_tool_calls:
        return []
    parsed: list[ToolCall] = []
    for tc in raw_tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        parsed.append(
            ToolCall(
                name=name,
                args=args if isinstance(args, dict) else {"_raw": args},
                id=tc.get("id"),
            )
        )
    return parsed


class ChatOpenAICompat(BaseChatModel):
    """LangChain-kompatibler Chat-Wrapper für OpenAI-Dialekt-Provider.

    Beispiel:
        llm = ChatOpenAICompat(provider="moonshot", model="moonshot-v1-32k")
        llm.invoke("ping")
    """

    provider: str = Field(default=DEFAULT_PROVIDER)
    model: str = Field(default="")
    base_url: str = Field(default="")
    temperature: float = Field(default=0.2)
    max_tokens: int = Field(default=2000)
    timeout: float = Field(default=120.0)
    api_key: Optional[str] = Field(default=None, repr=False)
    response_format: Optional[dict[str, Any]] = Field(default=None)
    tool_choice: Optional[str | dict[str, Any]] = Field(default=None)
    max_required_retries: int = Field(default=2)

    _bound_tools: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _force_tool_use: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> "ChatOpenAICompat":
        """Fill model/base_url from the registry and reject a non-OpenAI dialect."""
        prov = llm_providers.get(self.provider)
        if prov.dialect != "openai":
            raise DeepSeekError(
                f"{prov.label} does not speak the OpenAI dialect — "
                "use xbrl_sec.llm.factory.make_chat_model() instead."
            )
        object.__setattr__(self, "provider", prov.id)
        if not self.model:
            object.__setattr__(self, "model", prov.chat_model)
        object.__setattr__(self, "base_url", llm_providers.resolve_base_url(prov.id, self.base_url))
        return self

    @property
    def _provider(self) -> llm_providers.Provider:
        return llm_providers.get(self.provider)

    @property
    def _llm_type(self) -> str:
        return f"{self.provider}-chat"

    def _resolved_key(self) -> str:
        return self.api_key or resolve_env_key(self.provider)

    def _build_payload(self, messages: Sequence[BaseMessage], **kwargs: Any) -> dict[str, Any]:
        caps = self._provider.caps
        payload: dict[str, Any] = {
            "model": kwargs.get("model") or self.model,
            "messages": [_message_to_openai(m) for m in messages],
        }
        payload[caps.max_tokens_field] = llm_providers.clamp_max_tokens(
            self.provider, kwargs.get("max_tokens", self.max_tokens))
        if caps.temperature:
            payload["temperature"] = kwargs.get("temperature", self.temperature)
        tools = kwargs.get("tools") or self._bound_tools
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = _normalize_tool_choice(
                kwargs.get("tool_choice") or self.tool_choice or "auto"
            )
        response_format = kwargs.get("response_format") or self.response_format
        if response_format:
            payload["response_format"] = response_format
        if caps.disable_thinking:
            # Turn off Kimi's hidden reasoning so structured/tool output isn't starved by
            # the reasoning budget (verified: reasoning_tokens ~1 with this set).
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _prepare(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
        prov = self._provider
        api_key = self._resolved_key()
        if not api_key:
            raise DeepSeekError(
                f"No {prov.label} API key configured. Set {prov.env[0]} or pass api_key."
            )
        _validate_request(prov.id, self.base_url, str(payload.get("model") or ""))
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        return api_key, f"{self.base_url.rstrip('/')}/chat/completions", headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        prov = self._provider
        api_key, url, headers = self._prepare(payload)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DeepSeekError(f"Network error talking to {prov.label}: {exc}") from exc
        if response.status_code != 200:
            snippet = response.text[:500].replace(api_key, "***")
            raise DeepSeekError(f"{prov.label} returned {response.status_code}: {snippet}")
        return response.json()

    async def _apost(self, payload: dict[str, Any]) -> dict[str, Any]:
        prov = self._provider
        api_key, url, headers = self._prepare(payload)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DeepSeekError(f"Network error talking to {prov.label}: {exc}") from exc
        if response.status_code != 200:
            snippet = response.text[:500].replace(api_key, "***")
            raise DeepSeekError(f"{prov.label} returned {response.status_code}: {snippet}")
        return response.json()

    def _data_to_chat_result(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise DeepSeekError(f"{self._provider.label} returned no choices: {data}")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        usage = data.get("usage") or {}
        ai = AIMessage(
            content=content,
            tool_calls=tool_calls,
            response_metadata={
                "model_name": data.get("model"),
                "finish_reason": choices[0].get("finish_reason"),
                "token_usage": usage,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=ai)])

    def _enforce_tool_choice(
        self,
        payload: dict[str, Any],
        result: ChatResult,
        post_fn,
    ) -> ChatResult:
        """DeepSeek darf bei tool_choice='required' nicht mit reinem Text antworten.
        Wir re-prompten, schlägt es weiter fehl, failen wir hart."""
        if not self._force_tool_use:
            return result
        for _ in range(self.max_required_retries):
            ai = result.generations[0].message
            if isinstance(ai, AIMessage) and ai.tool_calls:
                return result
            payload = dict(payload)
            payload["messages"] = list(payload["messages"]) + [
                {
                    "role": "user",
                    "content": (
                        "You must call one of the provided tools to answer. "
                        "Do not reply with plain text. Issue exactly one tool call now."
                    ),
                }
            ]
            data = post_fn(payload)
            result = self._data_to_chat_result(data)
        ai = result.generations[0].message
        if not isinstance(ai, AIMessage) or not ai.tool_calls:
            raise DeepSeekError(
                f"{self._provider.label} refused to issue a tool call despite tool_choice='required'."
            )
        return result

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = self._build_payload(messages, **kwargs)
        if stop:
            payload["stop"] = stop
        data = self._post(payload)
        result = self._data_to_chat_result(data)
        return self._enforce_tool_choice(payload, result, self._post)

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = self._build_payload(messages, **kwargs)
        if stop:
            payload["stop"] = stop
        data = await self._apost(payload)
        result = self._data_to_chat_result(data)

        async def _post(p: dict[str, Any]) -> dict[str, Any]:
            return await self._apost(p)

        # _enforce_tool_choice is sync; replicate logic for async post here.
        if self._force_tool_use:
            for _ in range(self.max_required_retries):
                ai = result.generations[0].message
                if isinstance(ai, AIMessage) and ai.tool_calls:
                    return result
                payload = dict(payload)
                payload["messages"] = list(payload["messages"]) + [
                    {
                        "role": "user",
                        "content": (
                            "You must call one of the provided tools to answer. "
                            "Do not reply with plain text. Issue exactly one tool call now."
                        ),
                    }
                ]
                data = await _post(payload)
                result = self._data_to_chat_result(data)
            ai = result.generations[0].message
            if not isinstance(ai, AIMessage) or not ai.tool_calls:
                raise DeepSeekError(
                    f"{self._provider.label} refused to issue a tool call despite tool_choice='required'."
                )
        return result

    def bind_tools(
        self,
        tools: Sequence[BaseTool | Type[BaseModel] | dict[str, Any] | Any],
        *,
        tool_choice: Optional[str | dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Runnable:
        """Bind tools to the model. Accepts BaseTool, Pydantic schemas, or raw dicts."""
        converted = [convert_to_openai_tool(t) for t in tools]
        bound = self.bind(
            tools=converted,
            tool_choice=tool_choice or "auto",
            **kwargs,
        )
        # Track tool_choice on the underlying model for the enforcement loop.
        # Any value other than "auto"/"none" (e.g. "required", langchain's "any",
        # or a specific tool-name/dict) means the model must produce a tool call.
        forced = not (isinstance(tool_choice, str) and tool_choice in ("auto", "none")) and tool_choice is not None
        self._force_tool_use = forced
        self._bound_tools = converted
        return bound

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url}


class ChatDeepSeek(ChatOpenAICompat):
    """DeepSeek-pinned alias — the original class name, kept for existing callers.

    ``ChatDeepSeek(model="deepseek-v4-flash")`` behaves exactly as before. New
    code should go through :func:`xbrl_sec.llm.factory.make_chat_model`, which
    also covers Claude.
    """

    provider: str = Field(default="deepseek")
