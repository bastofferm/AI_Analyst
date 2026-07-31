"""AI Analyst FastAPI endpoints.

Reuses the sync `ai_analyst.tools` module (psycopg2 + pandas) via `asyncio.to_thread`
so we don't have to rewrite 14 read-only DB tools just to ship the chat surface.
The LLM HTTP calls themselves are native-async via httpx.AsyncClient.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ai import llm_runtime
from ..ai.prompts import CHAT_SYSTEM_PROMPT


router = APIRouter()
logger = logging.getLogger("mzqa.ai")


# Lazy-import ai_analyst.tools so the API doesn't crash at startup if the module
# is unavailable (it lives at the repo root, not under api/).
def _load_tools_module():
    from ai_analyst import tools as _tools  # type: ignore[import-not-found]
    return _tools


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    ticker: str | None = None
    jurisdiction: Literal["US", "JP"] | None = None
    manager_cik: str | None = None
    portfolio_holdings: list[dict[str, Any]] = Field(default_factory=list)
    etf_isin: str | None = None
    macro_jurisdiction: Literal["US", "JP", "EZ"] | None = None
    page: str | None = None
    lang: Literal["en", "de"] | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2000


class ChatResponse(BaseModel):
    text: str
    trace: list[dict]
    model: str
    has_env_key: bool


def _args_with_context_defaults(name: str, args: dict | None, req: ChatRequest) -> dict:
    """Apply page context only when a 13F tool call omitted its anchor.

    The AI Analyst panel is global, so context must never constrain explicit
    user intent. These defaults only rescue ambiguous "this company/manager"
    tool calls.
    """
    out = dict(args or {})
    if name == "get_institutional_holders" and not out.get("ticker") and req.ticker:
        out["ticker"] = req.ticker
    elif name == "compare_13f_ownership" and not out.get("tickers") and req.ticker:
        out["tickers"] = [req.ticker]
    elif name == "get_manager_portfolio" and not out.get("manager_cik") and req.manager_cik:
        out["manager_cik"] = req.manager_cik
    elif name in {"get_etf_detail", "get_etf_holdings_and_exposures"} and not out.get("isin") and req.etf_isin:
        out["isin"] = req.etf_isin
    elif name == "get_portfolio_etf_snapshot" and not out.get("holdings") and req.portfolio_holdings:
        out["holdings"] = req.portfolio_holdings
    elif name in {"get_macro_snapshot", "get_macro_calendar"} and not out.get("jurisdiction") and req.macro_jurisdiction:
        out["jurisdiction"] = req.macro_jurisdiction
    return out


@router.get("/status")
async def status() -> dict:
    """Reports whether an environment API key is available + the default model."""
    return {
        "has_env_key": bool(llm_runtime.resolve_env_key()),
        "default_chat_model": llm_runtime.DEFAULT_CHAT_MODEL,
        "default_reasoner": llm_runtime.DEFAULT_REASONER,
        "default_base_url": llm_runtime.DEFAULT_BASE_URL,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    api_key = (req.api_key or llm_runtime.resolve_env_key() or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No DeepSeek API key. Paste one in the AI panel's Settings tab "
                   "or set DEEPSEEK_API_KEY on the server.",
        )

    if not req.messages:
        raise HTTPException(status_code=400, detail="At least one message is required.")
    if req.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="The last message must be a user message.")

    try:
        tools_mod = _load_tools_module()
    except Exception as exc:
        logger.exception("Failed to import ai_analyst.tools")
        raise HTTPException(
            status_code=500,
            detail=f"AI Analyst tools unavailable: {exc.__class__.__name__}: {exc}",
        )

    async def tool_executor(name: str, args: dict) -> Any:
        # Sync tool runs on a worker thread; psycopg2 inside is blocking.
        return await asyncio.to_thread(
            tools_mod.execute,
            name,
            _args_with_context_defaults(name, args, req),
        )

    user_prompt = req.messages[-1].content
    history = [
        {"role": m.role, "content": m.content}
        for m in req.messages[:-1]
    ]

    ctx_bits = []
    if req.ticker:
        ctx_bits.append(f"default_ticker={req.ticker}")
    if req.jurisdiction:
        ctx_bits.append(f"jurisdiction={req.jurisdiction}")
    if req.manager_cik:
        ctx_bits.append(f"manager_cik={req.manager_cik}")
    if req.etf_isin:
        ctx_bits.append(f"current_etf_isin={req.etf_isin}")
    if req.macro_jurisdiction:
        ctx_bits.append(f"macro_jurisdiction={req.macro_jurisdiction}")
    if req.page:
        ctx_bits.append(f"page={req.page}")
    if req.lang:
        ctx_bits.append(f"lang={req.lang}")
    if req.portfolio_holdings:
        ctx_bits.append(
            "portfolio_holdings="
            + str([
                {
                    "isin": item.get("isin"),
                    "ticker": item.get("ticker"),
                    "weight": item.get("weight"),
                    "name": item.get("name"),
                }
                for item in req.portfolio_holdings[:12]
            ])
        )
    ctx_line = (" DASHBOARD CONTEXT: " + " ".join(ctx_bits)) if ctx_bits else ""
    system_prompt = CHAT_SYSTEM_PROMPT + ctx_line

    base_url = req.base_url or llm_runtime.DEFAULT_BASE_URL
    model = req.model or llm_runtime.DEFAULT_CHAT_MODEL

    try:
        text, trace = await llm_runtime.chat_with_tools(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools_mod.TOOLS,
            tool_executor=tool_executor,
            history=history,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
    except llm_runtime.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return ChatResponse(
        text=text,
        trace=trace,
        model=model,
        has_env_key=bool(llm_runtime.resolve_env_key()),
    )
