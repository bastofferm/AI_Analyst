"""Group ("relative-value verdict") committee endpoint.

Resolves a universe — a GICS industry (Industry tab) or an AI-screen prompt/filter
set (AI Screen tab) — to a capped ticker list using the existing screener, then runs
ONE relative-value committee deliberation over the group and returns a ranked verdict
plus a group thesis. The sync committee runs on a worker thread (same pattern as the
single-stock endpoint).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime
from .screener import (
    Range,
    ScreenerAiRequest,
    ScreenerRunRequest,
    Sort,
    Universe,
    screener_ai,
    screener_run,
)

router = APIRouter()
logger = logging.getLogger("mzqa.committee.group")


class GroupRequest(BaseModel):
    mode: Literal["industry", "screen"] = "industry"
    jurisdiction: Literal["US", "JP", "INTL"] = "US"
    country_code: str | None = None           # ISO-2, only meaningful when jurisdiction=INTL
    region: str | None = None                 # INTL region bucket (e.g. "Europe"), only meaningful when jurisdiction=INTL
    sectors: list[str] | None = None          # GICS sector codes (industry mode)
    industries: list[str] | None = None       # GICS industry-group codes (industry mode)
    filters: dict[str, Range] | None = None    # explicit screener filters (screen mode)
    prompt: str | None = None                  # NL prompt → filters via screener_ai (screen mode)
    tickers: list[str] | None = None           # explicit shortlist (e.g. from the value-sentiment agent)
    limit: int = Field(default=12, ge=2, le=25)
    provider: str | None = None      # llm_providers id; None -> server default (DeepSeek)
    api_key: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class GroupResponse(BaseModel):
    universe: dict[str, Any]
    resolved_tickers: list[str]
    ranking: list[dict[str, Any]]
    group_memo: str | None = None
    evidence: list[dict[str, Any]]
    report_html: str | None = None
    warnings: list[str] = Field(default_factory=list)


@router.post("/committee/group", response_model=GroupResponse)
async def committee_group(req: GroupRequest) -> GroupResponse:
    try:
        provider = llm_providers.normalize_id(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    warnings: list[str] = []

    # 1) Resolve the universe → filters/sort → rows.
    # Normalize INTL geography once so every branch uses the same values.
    country_code = req.country_code.strip().upper() if (req.country_code and req.jurisdiction == "INTL") else None
    region = req.region.strip() if (req.region and req.jurisdiction == "INTL") else None

    if req.tickers:
        # Explicit shortlist (value-sentiment agent, or a prompt-driven screen's union).
        # A shortlist is jurisdiction-agnostic: its names can come from any of the three
        # universes (US, JP, INTL), and a multi-model screen can even mix them. `jurisdiction`
        # is a TABLE selector in the screener (dim/fact_metrics_{us,jp,intl}), NOT a superset —
        # so re-resolving JP/US tickers under a single hardcoded jurisdiction returned zero rows.
        # (Ideas sends "INTL" as its "widest fallback"; that dropped every JP/US shortlist to
        # "No names matched".) Resolve against each jurisdiction and union instead.
        wanted = [t.upper() for t in req.tickers]
        by_ticker: dict[str, dict[str, Any]] = {}
        for juris in ("US", "JP", "INTL"):
            sub = await screener_run(ScreenerRunRequest(
                universe=Universe(jurisdiction=juris, portfolio_tickers=wanted),
                filters={}, sort=Sort(key="market_cap_usd", dir="desc"), limit=req.limit))
            for r in sub.rows:
                by_ticker.setdefault(r.ticker.upper(), r.model_dump())
        # Preserve the caller's order (the screens already ranked them), then cap.
        rows = [by_ticker[t] for t in wanted if t in by_ticker][: req.limit]
        # Label the group by the jurisdiction the names actually resolved to when that is
        # unambiguous; otherwise keep the caller's. Used only for the memo header.
        resolved_juris = {r["jurisdiction"] for r in rows}
        display_juris = resolved_juris.pop() if len(resolved_juris) == 1 else req.jurisdiction
        universe = Universe(jurisdiction=display_juris, country_code=country_code, region=region,
                            portfolio_tickers=wanted)
    else:
        if req.mode == "screen" and req.prompt and not req.filters:
            ai = await screener_ai(ScreenerAiRequest(
                prompt=req.prompt, jurisdiction=req.jurisdiction,
                provider=provider, api_key=api_key or None))
            # Prefer the geography the LLM inferred from the prompt; otherwise fall back
            # to the explicit request-level selection (dropdown).
            if req.jurisdiction == "INTL" and not ai.universe.country_code and country_code:
                ai.universe.country_code = country_code
            if req.jurisdiction == "INTL" and not ai.universe.region and region:
                ai.universe.region = region
            universe = ai.universe
            filters = ai.filters
            sort = ai.sort
            warnings.extend(ai.warnings)
        else:
            universe = Universe(jurisdiction=req.jurisdiction, country_code=country_code, region=region,
                                sectors=req.sectors, industries=req.industries)
            filters = req.filters or {}
            sort = Sort(key="market_cap_usd", dir="desc")

        # 2) Resolve → capped ticker set (deterministic screener).
        run = await screener_run(ScreenerRunRequest(
            universe=universe, filters=filters, sort=sort, limit=req.limit))
        rows = [r.model_dump() for r in run.rows]

    if not rows:
        raise HTTPException(status_code=422, detail="No names matched the group definition.")

    # 3) One relative-value committee deliberation (sync, on a worker thread).
    try:
        from ai_analyst.committee import group as group_mod  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to import ai_analyst.committee.group")
        raise HTTPException(status_code=500, detail=f"Group committee unavailable: {exc.__class__.__name__}: {exc}")

    try:
        result = await asyncio.to_thread(
            group_mod.run_group_committee,
            rows=rows, universe=universe.model_dump(), config=req.config,
            api_key=api_key or None, model=req.model, provider=provider,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Group committee run failed")
        raise HTTPException(status_code=502, detail=f"Group committee failed: {exc.__class__.__name__}: {exc}")

    return GroupResponse(
        universe=universe.model_dump(),
        resolved_tickers=[r["ticker"] for r in rows],
        ranking=result.get("ranking") or [],
        group_memo=result.get("group_memo"),
        evidence=rows,
        report_html=result.get("report_html"),
        warnings=warnings + (result.get("warnings") or []),
    )
