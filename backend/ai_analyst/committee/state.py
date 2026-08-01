"""GraphState for the investment-committee tribunal.

Mirrors the ``sec_daily.SECDailyState`` convention: ``TypedDict(total=False)`` with
an ``Annotated[..., reducer]`` field for the fan-in chat log. Bulk deterministic
data (the report packet) lives in ``financial_ratios``; the agents write prose to
their own slots.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict


class InvestmentCommitteeState(TypedDict, total=False):
    # --- Inputs ---
    ticker: str
    cik: str | None
    edinet_code: str | None
    jurisdiction: Literal["US", "JP", "INTL"]
    target_years: list[int]
    provider: str | None            # LLM provider id (llm_providers registry); None → server default
    api_key: str | None
    model: str | None
    config: dict[str, Any]             # CommitteeConfig (models, weights mode, toggles)

    # --- Phase 1: completeness & DQ (deterministic gate) ---
    completeness_report: dict[str, Any]
    is_data_complete: bool
    dq_errors: list[str]
    is_dq_passed: bool

    # --- Phase 2: financial analysis (deterministic) ---
    financial_ratios: dict[str, Any]   # == services.report_data_packet(ticker)
    mda_text: str
    segment_data: dict[str, Any]       # off-income-statement segment disclosures
    rich_filing_sections: dict[str, Any] # ranked XBRL HTML TextBlock disclosures with tables
    rich_filing_sections_compact: dict[str, Any] # prompt-ready rich filing evidence packet
    analytics: dict[str, Any]          # wacc, comps, cashflow history, incr ROIC, segment trend, prices, reverse DCF
    macro: dict[str, Any]              # macro context + signal
    news: dict[str, Any]               # scored sentiment (or macro-led fallback)
    ownership: dict[str, Any]          # 13F institutional summary
    evidence_bundle: dict[str, Any]    # per-run typed evidence cards/trees for auditability
    data_quality_report: dict[str, Any] # deterministic raw/std/metrics/recon/Yahoo DQ agent report
    data_quality_agent: dict[str, Any]  # DeepSeek triage + mapping evidence pack + proposals + deltas
    quant_signals: dict[str, Any]      # qlib alpha (expected return) + factor-structured risk + optimizer weight

    # --- Phase 3: tribunal debate ---
    advocate_analysis: str
    challenger_analysis: str
    auditor_analysis: str
    committee_chat_history: Annotated[list[dict[str, str]], operator.add]
    specialist_verdicts: Annotated[list[dict[str, Any]], operator.add]
    lead_synthesis: str
    scenarios: dict[str, Any]                  # consolidated DCF upside/base/downside
    dcf_model: dict[str, Any]                  # base-case full projected model (IS + FCFF + bridge)
    dcf_models: dict[str, Any]                 # {label: full model} for upside/base/downside
    sotp: dict[str, Any]                       # segment SOTP scenarios (primary)
    triangulation: dict[str, Any]              # SOTP-primary + DCF + multiples ranges
    reverse_dcf: dict[str, Any]                # market-implied growth
    primary_fair_value: float | None           # headline (SOTP-primary triangulated)
    probability_weighted_fair_value: float | None

    # --- Phase 4: output ---
    memo: dict[str, str]               # {"en": ..., "de": ...}

    # --- Control ---
    iteration_count: int
    decision_ready: bool
    errors: list[dict[str, str]]


def default_config() -> dict[str, Any]:
    """CommitteeConfig — parametrizes the LangGraph workflow. Overridable per run."""
    return {
        # DeepSeek aliases, kept as the historical defaults. When the run targets
        # another provider these are treated as "unset" (see nodes._LEGACY_DEEPSEEK
        # _MODELS) and that provider's registry models are used instead.
        # NB: deepseek-reasoner (and the deepseek-v4 "reasoning" tiers) spend the whole
        # max_tokens budget on hidden reasoning for the committee's large (~8k-token)
        # evidence-packet prompts and return EMPTY content (finish_reason=length),
        # which surfaces as every analyst falling back to a placeholder. deepseek-chat
        # answers these prompts directly (no runaway reasoning), so use it for narrative.
        "reasoning_model": "deepseek-chat",       # narrative/analysis (was deepseek-reasoner — see above)
        "structured_model": "deepseek-chat",      # reliable tool/JSON for scenario extraction
        "scenario_weight_mode": "macro_adjusted",  # "fixed" | "macro_adjusted"
        "base_weights": {"upside": 0.25, "base": 0.50, "downside": 0.25},
        "max_iterations": 3,
        "enable_news": True,
        "enable_13f": True,
        "enable_quant_signals": True,   # qlib alpha + risk evidence node (prepare phase)
        "enable_sotp": True,
        "enable_specialist_analysts": True,
        "specialist_analyst_mode": "auto",     # "auto" | "all" | "none" | comma/list of specialist keys
        "specialist_analysts": None,           # optional explicit roster override
        "max_specialist_analysts": None,       # default: all selected specialist archetypes
        "enable_specialist_structured_outputs": True,
        "sales_to_capital": 2.0,
        "erp_override_pct": None,
        # Data-governance: when False (default) accounting-identity DQ failures are
        # advisory — the committee still runs and surfaces the findings as a warning.
        # Set True (UI "strict" toggle) to hard-stop at the gate on any DQ failure.
        "dq_enforce": False,
        # Per-ticker DQ + mapping-check DeepSeek agent (parallel evidence node).
        "enable_data_quality_agent": True,   # run the node at all
        "dq_agent_always": False,            # force LLM triage even with no material findings
        "dq_agent_queue_proposals": True,    # write mapping proposals to the review queue
        "dq_agent_model": None,              # optional model override (else structured_model)
    }


def get_config(state: "InvestmentCommitteeState") -> dict[str, Any]:
    cfg = default_config()
    cfg.update(state.get("config") or {})
    return cfg


def record_error(state: InvestmentCommitteeState, stage: str, exc: Exception) -> list[dict[str, str]]:
    """Append a structured error, mirroring ``sec_daily._record_error``.

    Returns the new error list so a node can fold it into its return dict.
    """
    errors = list(state.get("errors") or [])
    errors.append({"stage": stage, "type": exc.__class__.__name__, "message": str(exc)[:300]})
    return errors
