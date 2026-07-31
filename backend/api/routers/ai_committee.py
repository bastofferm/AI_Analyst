"""Investment-committee FastAPI endpoint.

Runs the (sync, psycopg2 + httpx) committee LangGraph on a worker thread via
``asyncio.to_thread`` — the same pattern ``api/routers/ai_analyst.py`` uses for
its blocking DB tools. Returns the memo + probability-weighted fair value, or a
422 with the governance report if the completeness/DQ gate stopped the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime

router = APIRouter()
logger = logging.getLogger("mzqa.committee")


def _load_committee():
    from ai_analyst.committee import graph as committee_graph  # type: ignore[import-not-found]
    return committee_graph


def _render_report_html(state: dict[str, Any]) -> str | None:
    """Render the committee's self-contained HTML report (inline SVG) for the UI
    iframe. Non-fatal: the structured JSON fields are returned regardless."""
    try:
        from ai_analyst.committee import report_pdf  # type: ignore[import-not-found]
        return report_pdf.render_html(state)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to render committee report HTML")
        return None


def _final_evidence_bundle(state: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from ai_analyst import evidence as evidence_mod  # type: ignore[import-not-found]
        return evidence_mod.merge_runtime_context(
            state.get("evidence_bundle"),
            ticker=state.get("ticker"),
            jurisdiction=state.get("jurisdiction"),
            macro=state.get("macro"),
            news=state.get("news"),
            ownership=state.get("ownership"),
        ).model_dump(mode="json")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to prepare committee evidence bundle")
        bundle = state.get("evidence_bundle")
        return bundle if isinstance(bundle, dict) else None


class CommitteeRequest(BaseModel):
    ticker: str
    target_years: list[int] = Field(default_factory=list)
    provider: str | None = None      # llm_providers id; None -> server default (DeepSeek)
    api_key: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)  # CommitteeConfig overrides


class CommitteeIterateRequest(BaseModel):
    ticker: str
    user_comment: str
    current_result: dict[str, Any]
    iteration_history: list[dict[str, Any]] = Field(default_factory=list)
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_template_label: str | None = None


class CommitteeIterateResponse(BaseModel):
    iteration_number: int
    iteration_status: str = "completed"
    received_user_comment: str | None = None
    prompt_template_id: str | None = None
    prompt_template_label: str | None = None
    response_markdown: str
    revised_memo_en: str | None = None
    change_summary: str
    cited_evidence_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CommitteeResponse(BaseModel):
    ticker: str
    jurisdiction: str | None = None
    primary_fair_value: float | None = None            # SOTP-primary (headline)
    triangulation: dict[str, Any] | None = None        # SOTP + DCF + multiples ranges
    reverse_dcf: dict[str, Any] | None = None          # market-implied growth
    sotp: dict[str, Any] | None = None
    probability_weighted_fair_value: float | None = None  # consolidated DCF cross-check
    scenarios: dict[str, Any] | None = None
    analytics: dict[str, Any] | None = None            # sensitivity grid, price/quarterly/cashflow history, comps, wacc
    segment_data: dict[str, Any] | None = None
    rich_filing_sections: dict[str, Any] | None = None
    ownership: dict[str, Any] | None = None
    macro: dict[str, Any] | None = None
    evidence_bundle: dict[str, Any] | None = None
    data_quality_report: dict[str, Any] | None = None
    data_quality_agent: dict[str, Any] | None = None   # DeepSeek triage + mapping proposals + deltas
    mda_analysis: dict[str, Any] | None = None
    specialist_comments: list[dict[str, Any]] = Field(default_factory=list)
    yahoo_fundamentals: dict[str, Any] | None = None
    yahoo_cross_check: dict[str, Any] | None = None
    memo: dict[str, str] | None = None
    committee_chat_history: list[dict[str, str]] | None = None  # every analyst's thesis, incl. user-added
    specialist_verdicts: list[dict[str, Any]] | None = None      # structured specialist signals
    report_html: str | None = None                              # self-contained HTML report for the UI iframe
    completeness_report: dict[str, Any] | None = None
    dq_errors: list[str] = Field(default_factory=list)
    dq_warning: dict[str, Any] | None = None   # advisory gate detail when DQ failed but the run proceeded
    iteration_count: int | None = None


@router.post("/committee", response_model=CommitteeResponse)
async def committee(req: CommitteeRequest) -> CommitteeResponse:
    try:
        provider = llm_providers.normalize_id(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    if not req.ticker.strip():
        raise HTTPException(status_code=400, detail="ticker is required.")

    try:
        committee_graph = _load_committee()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to import ai_analyst.committee")
        raise HTTPException(status_code=500, detail=f"Committee unavailable: {exc.__class__.__name__}: {exc}")

    try:
        state = await asyncio.to_thread(
            _run,
            committee_graph, req.ticker, req.target_years, api_key or None, req.model, req.config,
            provider,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Committee run failed for %s", req.ticker)
        raise HTTPException(status_code=502, detail=f"Committee run failed: {exc.__class__.__name__}: {exc}")

    _raise_if_gated(state, bool((req.config or {}).get("dq_enforce")))
    return await _build_response(state, req.ticker, api_key, req.model, provider)


def _raise_if_gated(state: dict[str, Any], dq_enforce: bool) -> None:
    """Completeness is unanalyzable → hard 422. A DQ (accounting-identity) failure only
    blocks when the caller opted into strict mode (config.dq_enforce); otherwise the
    committee runs anyway and the caller gets 200 with an advisory dq_warning."""
    if state.get("is_data_complete") and not (dq_enforce and not state.get("is_dq_passed")):
        return
    raise HTTPException(
        status_code=422,
        detail={
            "message": "Data-governance gate failed; committee did not run.",
            "is_data_complete": state.get("is_data_complete"),
            "is_dq_passed": state.get("is_dq_passed"),
            "completeness_report": state.get("completeness_report"),
            "dq_errors": state.get("dq_errors"),
        },
    )


def _dq_warning(state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("is_dq_passed"):
        return None
    return {
        "is_data_complete": state.get("is_data_complete"),
        "is_dq_passed": state.get("is_dq_passed"),
        "completeness_report": state.get("completeness_report"),
        "dq_errors": state.get("dq_errors") or [],
    }


async def _build_response(
    state: dict[str, Any],
    ticker: str,
    api_key: str,
    model: str | None,
    provider: str | None,
) -> CommitteeResponse:
    """Post-graph tail shared by /committee and /debate. The MD&A read is an LLM
    call, so it belongs to whichever provider ran this debate."""
    dq_warning = _dq_warning(state)
    evidence_bundle = await asyncio.to_thread(_final_evidence_bundle, state)
    specialist_comments = await asyncio.to_thread(_specialist_comments, state)
    mda_analysis = await _mda_analysis(state, api_key, model, provider)
    report_state = dict(state)
    report_state["evidence_bundle"] = evidence_bundle
    report_state["data_quality_report"] = state.get("data_quality_report")
    report_state["specialist_comments"] = specialist_comments
    report_state["mda_analysis"] = mda_analysis
    report_html = await asyncio.to_thread(_render_report_html, report_state)
    packet = state.get("financial_ratios") or {}

    return CommitteeResponse(
        ticker=ticker,
        jurisdiction=state.get("jurisdiction"),
        primary_fair_value=state.get("primary_fair_value"),
        triangulation=state.get("triangulation"),
        reverse_dcf=state.get("reverse_dcf"),
        sotp=state.get("sotp"),
        probability_weighted_fair_value=state.get("probability_weighted_fair_value"),
        scenarios=state.get("scenarios"),
        analytics=state.get("analytics"),
        segment_data=state.get("segment_data"),
        rich_filing_sections=state.get("rich_filing_sections"),
        ownership=state.get("ownership"),
        macro=state.get("macro"),
        evidence_bundle=evidence_bundle,
        data_quality_report=state.get("data_quality_report"),
        data_quality_agent=state.get("data_quality_agent"),
        mda_analysis=mda_analysis,
        specialist_comments=specialist_comments,
        yahoo_fundamentals=packet.get("yahoo_fundamentals"),
        yahoo_cross_check=packet.get("yahoo_cross_check"),
        memo=state.get("memo"),
        committee_chat_history=state.get("committee_chat_history"),
        specialist_verdicts=state.get("specialist_verdicts"),
        report_html=report_html,
        completeness_report=state.get("completeness_report"),
        dq_errors=state.get("dq_errors") or [],
        dq_warning=dq_warning,
        iteration_count=state.get("iteration_count"),
    )


# ------------------------------------------------------- prepare / debate (multi-provider)
#
# The deterministic phase (gate → engine → evidence) is provider-independent, so
# running it once and letting N providers debate on top of it costs one pass
# instead of N. It also means every provider argues over *identical* evidence,
# which is what makes comparing their verdicts meaningful.
#
# The prepared state is far too large to round-trip through the browser (full
# report packet, rich filing sections, price history), so it is held here and
# referenced by id. That assumes the single-worker uvicorn this app runs; under
# multiple workers a follow-up /debate could land on a worker that never saw the
# id. Rather than pretend otherwise, an unknown id answers 409 and the client
# simply re-prepares — which also covers a tab left open past the TTL.

_PREPARED_TTL_SECONDS = 30 * 60
_PREPARED_MAX_ENTRIES = 8
_prepared_states: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()


def _prune_prepared(now: float) -> None:
    for pid in [p for p, (ts, _) in _prepared_states.items() if now - ts > _PREPARED_TTL_SECONDS]:
        _prepared_states.pop(pid, None)


def _store_prepared(state: dict[str, Any]) -> tuple[str, float]:
    now = time.time()
    _prune_prepared(now)
    prepared_id = uuid.uuid4().hex
    _prepared_states[prepared_id] = (now, state)
    while len(_prepared_states) > _PREPARED_MAX_ENTRIES:
        _prepared_states.popitem(last=False)   # oldest out first
    return prepared_id, now + _PREPARED_TTL_SECONDS


def _load_prepared(prepared_id: str) -> dict[str, Any]:
    _prune_prepared(time.time())
    entry = _prepared_states.get(prepared_id)
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "That prepared analysis is no longer available; run the preparation step again.",
                "reason": "prepared_expired",
            },
        )
    return entry[1]


class PrepareRequest(BaseModel):
    ticker: str
    target_years: list[int] = Field(default_factory=list)
    provider: str | None = None      # runs the DQ-triage node; the debate provider is per /debate call
    api_key: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class PrepareResponse(BaseModel):
    prepared_id: str
    ticker: str
    jurisdiction: str | None = None
    expires_at: float
    dq_warning: dict[str, Any] | None = None


class DebateRequest(BaseModel):
    prepared_id: str
    ticker: str
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None


@router.post("/committee/prepare", response_model=PrepareResponse)
async def committee_prepare(req: PrepareRequest) -> PrepareResponse:
    """Phase 1: gather the shared evidence base for one ticker.

    A governance failure stops here with the same 422 the one-shot endpoint
    returns — so a gated ticker costs one run rather than one per provider.
    """
    try:
        provider = llm_providers.normalize_id(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    if not req.ticker.strip():
        raise HTTPException(status_code=400, detail="ticker is required.")

    try:
        committee_graph = _load_committee()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to import ai_analyst.committee")
        raise HTTPException(status_code=500, detail=f"Committee unavailable: {exc.__class__.__name__}: {exc}")

    try:
        state = await asyncio.to_thread(
            committee_graph.run_prepare,
            req.ticker, req.target_years,
            provider=provider, api_key=api_key or None, model=req.model, config=req.config or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Committee preparation failed for %s", req.ticker)
        raise HTTPException(status_code=502, detail=f"Preparation failed: {exc.__class__.__name__}: {exc}")

    _raise_if_gated(state, bool((req.config or {}).get("dq_enforce")))
    prepared_id, expires_at = _store_prepared(state)
    return PrepareResponse(
        prepared_id=prepared_id,
        ticker=req.ticker,
        jurisdiction=state.get("jurisdiction"),
        expires_at=expires_at,
        dq_warning=_dq_warning(state),
    )


@router.post("/committee/debate", response_model=CommitteeResponse)
async def committee_debate(req: DebateRequest) -> CommitteeResponse:
    """Phase 2: one provider's debate over a prepared state.

    Safe to call concurrently for several providers against the same
    ``prepared_id`` — ``run_debate`` deep-copies before invoking the graph.
    """
    try:
        provider = llm_providers.normalize_id(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    prepared = _load_prepared(req.prepared_id)

    try:
        committee_graph = _load_committee()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to import ai_analyst.committee")
        raise HTTPException(status_code=500, detail=f"Committee unavailable: {exc.__class__.__name__}: {exc}")

    try:
        state = await asyncio.to_thread(
            committee_graph.run_debate,
            prepared, provider=provider, api_key=api_key or None, model=req.model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Committee debate failed for %s on %s", req.ticker, provider)
        raise HTTPException(status_code=502, detail=f"Debate failed: {exc.__class__.__name__}: {exc}")

    return await _build_response(state, req.ticker, api_key, req.model, provider)


class PromoteMappingRequest(BaseModel):
    jurisdiction: str
    concept_id: str
    mapping_sector: str | None = None
    target_variable: str | None = None


class PromoteMappingResponse(BaseModel):
    status: str
    action: str | None = None            # "inserted" | "updated"
    mapping_id: int | None = None
    concept_id: str | None = None
    target_variable: str | None = None
    mapping_sector: str | None = None
    jurisdiction: str | None = None


@router.post("/committee/promote_mapping", response_model=PromoteMappingResponse)
async def promote_mapping(req: PromoteMappingRequest) -> PromoteMappingResponse:
    """Promote one queued committee_dq_agent mapping proposal into the governed mapping
    table (map_concept_to_taxonomy_versioned) and mark the queue row approved."""
    if not req.concept_id.strip():
        raise HTTPException(status_code=400, detail="concept_id is required.")
    from ai_analyst.mapping_promote import PromoteError, promote_proposal  # type: ignore[import-not-found]

    try:
        result = await asyncio.to_thread(
            promote_proposal,
            jurisdiction=req.jurisdiction,
            concept_id=req.concept_id,
            mapping_sector=req.mapping_sector,
            target_variable=req.target_variable,
        )
    except PromoteError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("promote_mapping failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    return PromoteMappingResponse(**result)


@router.post("/committee/iterate", response_model=CommitteeIterateResponse)
async def committee_iterate(req: CommitteeIterateRequest) -> CommitteeIterateResponse:
    comment = req.user_comment.strip()
    if not req.ticker.strip():
        raise HTTPException(status_code=400, detail="ticker is required.")
    if not comment:
        raise HTTPException(status_code=400, detail="user_comment is required.")

    from ai_analyst import committee_revision  # type: ignore[import-not-found]

    iteration_number = len(req.iteration_history or []) + 1
    try:
        provider = llm_providers.normalize_id(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    api_key = (req.api_key or llm_runtime.resolve_env_key(provider) or "").strip()
    if not api_key or _llm_disabled():
        return CommitteeIterateResponse(**committee_revision.no_key_iteration_response(
            iteration_number,
            comment,
            prompt_template_id=req.prompt_template_id,
            prompt_template_label=req.prompt_template_label,
        ))

    current = committee_revision.compact_current_result(req.current_result or {})
    model = llm_providers.chat_model(provider, req.model)
    system = (
        "You are the lead analyst revising an existing investment committee output. "
        "The fact base is frozen: do not reload data, recompute valuation, or invent new facts. "
        "Use only the supplied current_result, evidence IDs, data-quality report, MD&A analysis, "
        "specialist comments, and prior revision history. Treat the user comment between the "
        "USER COMMENT START/END delimiters as the controlling revision instruction. Return JSON "
        "with response_markdown, revised_memo_en, change_summary, cited_evidence_ids, and warnings. "
        "Keep response_markdown concise and set revised_memo_en to null unless the user explicitly "
        "asks for a full memo rewrite."
    )
    user = (
        f"TICKER: {req.ticker.upper()}\n"
        f"PROMPT TEMPLATE ID: {req.prompt_template_id or ''}\n"
        f"PROMPT TEMPLATE LABEL: {req.prompt_template_label or ''}\n\n"
        "USER COMMENT START\n"
        f"{comment}\n"
        "USER COMMENT END\n\n"
        "CURRENT FROZEN RESULT JSON:\n"
        + json.dumps(current, default=str)[:28000]
        + "\n\nPRIOR REVISION HISTORY JSON:\n"
        + json.dumps(req.iteration_history or [], default=str)[:8000]
    )
    try:
        data = await llm_runtime.chat_json(
            api_key=api_key,
            provider=provider,
            model=model,
            system_prompt=system,
            user_prompt=user,
            temperature=0.15,
            max_tokens=5000,
        )
    except llm_runtime.LLMError as exc:
        logger.warning("Revision iteration fell back for %s: %s", req.ticker, exc)
        return CommitteeIterateResponse(**committee_revision.iteration_fallback_response(
            iteration_number,
            comment,
            f"Revision model call failed: {exc}",
            prompt_template_id=req.prompt_template_id,
            prompt_template_label=req.prompt_template_label,
        ))

    fallback = "Revision addendum could not be parsed, but the current committee output remains unchanged."
    return CommitteeIterateResponse(
        **committee_revision.normalize_iteration_response(
            data,
            iteration_number=iteration_number,
            fallback_markdown=fallback,
            user_comment=comment,
            prompt_template_id=req.prompt_template_id,
            prompt_template_label=req.prompt_template_label,
        )
    )


def _run(committee_graph, ticker, target_years, api_key, model, config, provider=None):
    return committee_graph.run_committee(ticker, target_years, api_key=api_key, model=model,
                                         config=config or None, provider=provider)


def _specialist_comments(state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from ai_analyst import committee_comments  # type: ignore[import-not-found]
        return committee_comments.build_specialist_comments(state)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to prepare specialist comments")
        return []


async def _mda_analysis(state: dict[str, Any], api_key: str, model: str | None,
                        provider: str | None = None) -> dict[str, Any]:
    try:
        from ai_analyst import mda_guidance  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return {"warnings": [f"MD&A guidance unavailable: {exc.__class__.__name__}"]}

    packet = state.get("financial_ratios") or {}
    company = packet.get("company") or {}
    ticker = str(state.get("ticker") or company.get("ticker") or "").upper()
    jurisdiction = str(state.get("jurisdiction") or company.get("jurisdiction") or "US").upper()
    peer_rows = ((packet.get("peer_group") or {}).get("peers") or [])[: mda_guidance.MAX_PEERS]
    peer_items = [
        {
            "ticker": str(peer.get("ticker") or "").upper(),
            "jurisdiction": str(peer.get("jurisdiction") or jurisdiction).upper(),
        }
        for peer in peer_rows
        if isinstance(peer, dict) and peer.get("ticker")
    ]
    peer_tickers = [item["ticker"] for item in peer_items]
    state_mda = str(state.get("mda_text") or "").strip()

    if not api_key or _llm_disabled():
        return mda_guidance.no_key_analysis(ticker, jurisdiction, state_mda, len(peer_tickers))

    items = [{"ticker": ticker, "jurisdiction": jurisdiction}, *peer_items]
    fetched = await asyncio.to_thread(mda_guidance.fetch_recent_mda_texts, items)
    target_text = (fetched.get(ticker) or {}).get("text") or state_mda
    if not target_text:
        return mda_guidance.base_analysis(
            ticker,
            jurisdiction,
            target_text,
            warnings=["No MD&A text found for analyzed company."],
            peer_count=len(peer_tickers),
        )

    missing_peers = [peer for peer in peer_tickers if not (fetched.get(peer) or {}).get("text")]
    warnings = []
    if missing_peers:
        warnings.append(f"MD&A text missing for {len(missing_peers)} peer(s).")

    records = [
        {
            "ticker": ticker,
            "role": "target",
            "text": target_text,
            "filings": (fetched.get(ticker) or {}).get("filings") or [],
        }
    ]
    for peer in peer_items:
        fetched_peer = fetched.get(peer["ticker"]) or {}
        text = fetched_peer.get("text")
        if text:
            records.append({"ticker": peer["ticker"], "role": "peer", "text": text, "filings": fetched_peer.get("filings") or []})
    prompt = mda_guidance.build_mda_user_prompt(records)
    try:
        data = await llm_runtime.chat_json(
            api_key=api_key,
            provider=provider,
            model=llm_providers.chat_model(provider, model),
            system_prompt=mda_guidance.MDA_SYSTEM_PROMPT,
            user_prompt=prompt,
            temperature=0.1,
            max_tokens=5000,
        )
    except llm_runtime.LLMError as exc:
        return mda_guidance.base_analysis(
            ticker,
            jurisdiction,
            target_text,
            warnings=[*warnings, f"MD&A scoring failed: {exc}"],
            peer_count=len(peer_tickers),
        )

    return mda_guidance.analysis_from_llm_response(
        ticker=ticker,
        jurisdiction=jurisdiction,
        llm_data=data,
        peer_tickers=peer_tickers,
        mda_text=target_text,
        warnings=warnings,
    )


def _llm_disabled() -> bool:
    return os.environ.get("MZQA_COMMITTEE_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
