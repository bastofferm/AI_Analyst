"""Committee node functions.

Deterministic gate (completeness + DQ) runs before any LLM, mirroring the
"garbage-in never reaches the agents" discipline of the ETL graphs. The tribunal
and lead nodes use the run's configured provider for structured output; if no
usable key is available they degrade to deterministic placeholders so the
gate/segment/valuation paths remain runnable and testable offline.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import llm_providers

from .. import data_quality_agent
from .. import dq_triage
from .. import evidence as evidence_mod
from .. import llm_runtime
from .. import rich_filing_sections
from .. import services
from ..dcf_engine import DCF_HORIZON_YEARS
from .._db import read_sql
from . import (archetypes, comps, institutional, marketdata, metrics as metrics_mod,
               model as model_mod, newsmacro, prompts, quarterly, segments,
               valuation, wacc as wacc_mod)
from .schemas import (AgentThesis, CommitteeMemo, CommitteeVerdict,
                      ScenarioAssumptions, SpecialistVerdict)
from .state import InvestmentCommitteeState, get_config, record_error

logger = logging.getLogger("mzqa.committee")

_MDA_CAP = 6000
_MAX_ITERATIONS = 3


# --------------------------------------------------------------------------- LLM
# Two-tier: reasoning_model (e.g. deepseek-reasoner / claude-opus-4-8 with adaptive
# thinking) for narrative/analysis via plain text; structured_model (e.g.
# deepseek-chat) for tool/JSON extraction. DeepSeek's reasoner is a thinking-mode
# model that does NOT reliably do function-calling, so scenario extraction stays on
# the chat model. The provider comes from state["provider"] (llm_providers registry).

# default_config() still carries the historical DeepSeek aliases. Treat them as
# "unset" when the run targets another provider — otherwise a Claude run would be
# sent a `deepseek-chat` model ID and 404. An explicitly configured model that is
# not one of these always wins.
_LEGACY_DEEPSEEK_MODELS = {"deepseek-chat", "deepseek-reasoner"}


def _provider(state: InvestmentCommitteeState) -> str:
    return llm_providers.normalize_id(state.get("provider"))


def _configured_model(state: InvestmentCommitteeState, config_key: str) -> str | None:
    configured = state.get("model") or get_config(state).get(config_key)
    if not configured:
        return None
    if _provider(state) != llm_providers.DEFAULT_PROVIDER and configured in _LEGACY_DEEPSEEK_MODELS:
        return None
    return str(configured)


def _resolve_key(state: InvestmentCommitteeState) -> str:
    # MZQA_COMMITTEE_DISABLE_LLM forces the deterministic (no-LLM) path — useful for
    # offline runs, CI, and reproducible verification without spending tokens.
    if os.environ.get("MZQA_COMMITTEE_DISABLE_LLM", "").strip() in {"1", "true", "yes"}:
        return ""
    return (state.get("api_key") or llm_providers.resolve_env_key(_provider(state)) or "").strip()


def _structured_model(state: InvestmentCommitteeState) -> str:
    return llm_providers.chat_model(_provider(state), _configured_model(state, "structured_model"))


def _reasoning_model(state: InvestmentCommitteeState) -> str:
    return llm_providers.reasoner_model(_provider(state), _configured_model(state, "reasoning_model"))


def _make_structured(state: InvestmentCommitteeState, schema, *, temperature: float, max_tokens: int):
    from xbrl_sec.llm import make_chat_model, setup_llm_cache
    setup_llm_cache()
    llm = make_chat_model(
        _provider(state),
        _structured_model(state),
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=state.get("api_key") or None,
    )
    return llm.with_structured_output(schema)


def _invoke_structured(state: InvestmentCommitteeState, schema, prompt: str, *,
                       temperature: float, max_tokens: int, attempts: int = 3):
    """Invoke a structured-output call with retries — DeepSeek's function-calling
    occasionally returns malformed/empty tool args on the first try."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            result = _make_structured(state, schema, temperature=temperature, max_tokens=max_tokens).invoke(prompt)
            if result is not None:
                return result
            last = ValueError("structured output returned None")
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("structured call (%s) attempt %d/%d failed: %s",
                           getattr(schema, "__name__", schema), i + 1, attempts, exc)
        time.sleep(1.2 * (i + 1))
    raise last or RuntimeError("structured invoke failed")


# ----------------------------------------------------------------- Node 1: gate

def completeness_check_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    ticker = state["ticker"]
    overview = services.company_overview(ticker)
    if not overview.get("found"):
        return {
            "completeness_report": {"ticker": ticker, "found": False},
            "is_data_complete": False,
            "iteration_count": 0,
            "jurisdiction": state.get("jurisdiction") or "US",
        }
    jurisdiction = overview["jurisdiction"]
    cik = overview.get("cik")
    edinet_code = overview.get("edinet_code")
    target_years = state.get("target_years") or []

    # INTL branch: no source_filing_state coverage (no XBRL filings tracked). Gate
    # on Yahoo statement rows only via the reduced modeled snapshot.
    if jurisdiction == "INTL":
        snapshot = services.modeled_statement_snapshot_intl(ticker, years=max(5, len(target_years) or 5))
        fund_years = sorted({int(r["fiscal_year"]) for r in snapshot["rows"] if r.get("fiscal_year") is not None})
        missing_years = [y for y in target_years if y not in fund_years] if target_years else []
        is_complete = (not missing_years) and bool(fund_years)
        report = {
            "ticker": ticker,
            "jurisdiction": "INTL",
            "target_years": target_years,
            "fundamental_years_present": fund_years,
            "parsed_filing_years": [],
            "missing_fundamental_years": missing_years,
            "filings_tracked": 0,
            "note": "INTL: no XBRL filings tracked; gate is Yahoo annual statement coverage.",
        }
        return {
            "jurisdiction": "INTL",
            "cik": None,
            "edinet_code": None,
            "completeness_report": report,
            "is_data_complete": is_complete,
            "iteration_count": 0,
        }

    # Standardized fundamentals are the real gate for the quantitative engine.
    snapshot = services.modeled_statement_snapshot(ticker, years=max(5, len(target_years) or 5))
    fund_years = sorted({int(r["fiscal_year"]) for r in snapshot["rows"] if r.get("fiscal_year") is not None})

    # Staging truth: parsed filings per period from source_filing_state.
    entity_id = str(cik).zfill(10) if jurisdiction == "US" else edinet_code
    filings = read_sql(
        """
        SELECT filing_type, filed_date, period_end,
               EXTRACT(YEAR FROM period_end)::int AS period_year,
               COALESCE(parsed, FALSE) AS parsed
        FROM source_filing_state
        WHERE jurisdiction = %(j)s AND entity_id = %(eid)s
        """,
        {"j": jurisdiction, "eid": entity_id},
    )
    parsed_years = sorted({
        int(r["period_year"]) for r in filings.to_dict("records")
        if r.get("parsed") and r.get("period_year") is not None and r["period_year"] == r["period_year"]
    })

    missing_years = [y for y in target_years if y not in fund_years] if target_years else []
    is_complete = (not missing_years) and bool(fund_years)

    report = {
        "ticker": ticker,
        "jurisdiction": jurisdiction,
        "target_years": target_years,
        "fundamental_years_present": fund_years,
        "parsed_filing_years": parsed_years,
        "missing_fundamental_years": missing_years,
        "filings_tracked": int(len(filings)),
    }
    return {
        "jurisdiction": jurisdiction,
        "cik": str(cik).zfill(10) if cik is not None else None,
        "edinet_code": edinet_code,
        "completeness_report": report,
        "is_data_complete": is_complete,
        "iteration_count": 0,
    }


# The brief's hard constraints: accounting equation, cash roll-forward, and the
# structural balance-sheet/cash sub-identities. Only these gate the pipeline;
# minor per-line-item `rollup:*` checks are reported as warnings but do not block.
_CORE_IDENTITY_CHECKS = frozenset({
    "accounting_equation",
    "assets_current_plus_noncurrent",
    "liabilities_current_plus_noncurrent",
    "cash_bridge",
    "net_change_identity",
})


def dq_validation_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    ticker = state["ticker"]
    # INTL: no XBRL raw layer means no accounting-identity checks to run. Return
    # passing with a note so downstream nodes proceed but the memo shows the caveat.
    if state.get("jurisdiction") == "INTL":
        return {"is_dq_passed": True, "dq_errors": [],
                "dq_warning": "INTL: accounting-identity DQ gate skipped (no XBRL raw layer)."}
    # Tolerance is the number of *core* identity violations allowed before the gate
    # trips. A caller (e.g. the UI's "run anyway" override) may raise it per-run via
    # config["dq_tolerance"]; otherwise fall back to the env var, then 0 (strict).
    cfg_tol = get_config(state).get("dq_tolerance")
    try:
        tolerance = int(cfg_tol) if cfg_tol is not None else _int_env("MZQA_COMMITTEE_DQ_TOLERANCE", 0)
    except (TypeError, ValueError):
        tolerance = _int_env("MZQA_COMMITTEE_DQ_TOLERANCE", 0)
    target_years = set(state.get("target_years") or [])
    try:
        snapshot = services.modeled_statement_snapshot(ticker, years=max(5, len(target_years) or 5))
    except Exception as exc:  # noqa: BLE001
        return {"is_dq_passed": False, "dq_errors": [f"snapshot failed: {exc}"],
                "errors": record_error(state, "dq_validation", exc)}

    errors: list[str] = []
    warnings: list[str] = []
    core_violations = 0
    for row in snapshot["rows"]:
        fy = row.get("fiscal_year")
        if target_years and fy not in target_years:
            continue
        checks = row.get("identity_violation_checks") or []
        if not checks:
            continue
        core = [c for c in checks if str(c).split(":")[0] in _CORE_IDENTITY_CHECKS or str(c) in _CORE_IDENTITY_CHECKS]
        minor = [c for c in checks if c not in core]
        if core:
            core_violations += len(core)
            errors.append(f"FY{fy} {row.get('line_item_id')}: CORE identity break [{', '.join(map(str, core))}]")
        if minor:
            warnings.append(f"FY{fy} {row.get('line_item_id')}: rollup warning [{', '.join(map(str, minor))}]")

    try:
        recon = services.recon_flags(ticker)
        bad = [r for r in recon["rows"]
               if str(r.get("trace_quality") or "").lower() in {"broken", "bad", "fail", "red"}]
        for r in bad[:10]:
            warnings.append(f"recon {r.get('fiscal_year')} {r.get('metric_id')}: trace_quality={r.get('trace_quality')}")
    except Exception:  # noqa: BLE001 - recon is advisory
        pass

    is_passed = core_violations <= tolerance
    # dq_errors carries core breaks first (they gate), then advisory warnings.
    return {"is_dq_passed": is_passed, "dq_errors": errors + warnings[:20]}


# --------------------------------------------------------------- Node 3: engine

def financial_analysis_engine_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    ticker = state["ticker"]
    jurisdiction = state.get("jurisdiction") or "US"
    try:
        packet = services.report_data_packet(ticker)
    except Exception as exc:  # noqa: BLE001
        return {"errors": record_error(state, "engine", exc),
                "financial_ratios": {}, "mda_text": "", "segment_data": segments._empty(note=str(exc))}

    # INTL: no MDA text, no rich filing sections, no segments — Yahoo doesn't provide any.
    if jurisdiction == "INTL":
        mda_text = ""
        seg = segments._empty(note="INTL: no segment data (Yahoo-backed).")
        rich_sections = {}
    else:
        mda_text = _load_mda(state.get("cik"), state.get("edinet_code"), jurisdiction)
        seg = segments.extract_segments(
            state.get("cik") if jurisdiction == "US" else state.get("edinet_code"),
            jurisdiction,
            state.get("target_years"),
        )
        rich_sections = _load_rich_filing_sections(ticker, jurisdiction, state.get("target_years"))
    analytics = _compute_analytics(state, packet, seg, jurisdiction)
    # INTL entity_id is the packet's overview.uid (intl_company_id::text); US/JP unchanged.
    if jurisdiction == "INTL":
        entity_id = (packet.get("company") or {}).get("uid")
        dq_report = {
            "ticker": ticker, "jurisdiction": "INTL", "entity_id": entity_id,
            "as_of": None, "overall_score": None, "layer_scores": {}, "counts": {},
            "findings": [], "metric_reconciliations": [], "coverage_gaps": {},
            "repair_suggestions": [],
            "warnings": ["INTL: XBRL raw/std/recon layers are absent — DQ agent not run."],
        }
    else:
        entity_id = state.get("cik") if jurisdiction == "US" else state.get("edinet_code")
        dq_report = data_quality_agent.build_data_quality_report(
            ticker=ticker,
            jurisdiction=jurisdiction,
            entity_id=entity_id,
            packet=packet,
            completeness_report=state.get("completeness_report") or {},
            dq_errors=state.get("dq_errors") or [],
        ).model_dump(mode="json")
    evidence_bundle = evidence_mod.build_evidence_bundle(
        ticker=ticker,
        jurisdiction=jurisdiction,
        entity_id=entity_id,
        packet=packet,
        mda_text=mda_text,
        segment_data=seg,
        rich_filing_sections=rich_sections,
        analytics=analytics,
        data_quality_report=dq_report,
    ).model_dump(mode="json")
    return {
        "financial_ratios": packet,
        "mda_text": mda_text,
        "segment_data": seg,
        "rich_filing_sections": rich_sections,
        "rich_filing_sections_compact": rich_sections.get("compact") if isinstance(rich_sections, dict) else {},
        "analytics": analytics,
        "evidence_bundle": evidence_bundle,
        "data_quality_report": dq_report,
    }


def _compute_analytics(state, packet, seg, jurisdiction) -> dict[str, Any]:
    """All deterministic quant that the tribunal argues over and the report renders."""
    ticker = state["ticker"]
    cik = state.get("cik")
    out: dict[str, Any] = {}
    if jurisdiction == "INTL":
        # No factor loadings for INTL; use sector-default WACC from ref_wacc_sector_default.
        modeled = packet.get("modeled_statements") or {}
        sector_scope = modeled.get("sector_scope") or "corp"
        wacc_pct = services._load_sector_default_wacc(sector_scope)
        w = {"wacc_pct": wacc_pct, "source": "sector_default", "sector_scope": sector_scope,
             "note": f"INTL: sector-default WACC ({sector_scope}={wacc_pct}%). Wide uncertainty band."}
        out["wacc"] = w
    else:
        try:
            w = wacc_mod.compute_wacc(ticker, packet)
            out["wacc"] = w
        except Exception as exc:  # noqa: BLE001
            w = {"wacc_pct": 9.0}
            out["wacc_error"] = str(exc)[:200]
    hist: list[dict[str, Any]] = []
    if jurisdiction == "INTL":
        # US/JP-specific quant is skipped for INTL. Comps → peer_group_intl (already
        # populated on packet); cashflow history → derive from reduced modeled statements
        # (rough, but sufficient for the DCF).
        out["comps"] = {"available": False, "note": "INTL: GICS-clustered comps unavailable."}
        out["segment_trend"] = {}
        out["cashflow_history"] = []
        out["incremental_roic"] = {"available": False, "note": "INTL: needs multi-year fact_metrics_us peer set."}
    else:
        try:
            out["comps"] = comps.build_comps(ticker, packet)
        except Exception as exc:  # noqa: BLE001
            out["comps_error"] = str(exc)[:200]
        try:
            hist = marketdata.financial_history(cik, jurisdiction, years=6)
            out["cashflow_history"] = hist
            out["incremental_roic"] = marketdata.incremental_roic(hist, w.get("wacc_pct", 9.0))
        except Exception as exc:  # noqa: BLE001
            out["cashflow_error"] = str(exc)[:200]
        try:
            out["segment_trend"] = segments.extract_segment_trend(cik, jurisdiction)
        except Exception:  # noqa: BLE001
            out["segment_trend"] = {}

    # Market data for the price chart: company + top-5 largest GICS peers.
    peers = []
    comps_data = out.get("comps") or {}
    for r in (comps_data.get("sector_peers") or [])[:5]:
        if r.get("ticker"):
            peers.append(r["ticker"])
    try:
        out["price_history"] = marketdata.price_history([ticker] + peers, years=3)
        out["price_peers"] = peers
    except Exception:  # noqa: BLE001
        out["price_history"] = {}

    # Quarterly TTM / trend series (fresher than fiscal-year; also feeds the report
    # exhibit, canonical multiples, and the DCF base). Guarded for filers without
    # clean quarterly data (e.g. 20-F foreign filers).
    try:
        out["quarterly"] = quarterly.quarterly_series(cik, jurisdiction)
    except Exception as exc:  # noqa: BLE001
        out["quarterly"] = {"available": False, "note": str(exc)[:200]}
    _ttm = (out.get("quarterly") or {}).get("ttm") or {}
    ttm = _ttm if _ttm.get("available") else None

    # Base assumptions (for sensitivity + reverse DCF), anchored to history + TTM.
    base = _base_assumptions(packet, hist, w, ttm=ttm)
    out["base_assumptions"] = base
    price = _stock_price(packet)
    out["current_price"] = price
    out["shares"] = (w.get("market_cap") / price) if (w.get("market_cap") and price) else None
    out["net_debt"] = (packet.get("dcf") or {}).get("historicals_used", {}).get("net_debt") or 0.0
    try:
        out["sensitivity_grid"] = valuation.sensitivity_grid(ticker, base)
    except Exception:  # noqa: BLE001
        out["sensitivity_grid"] = {}
    try:
        if price:
            out["reverse_dcf"] = valuation.reverse_dcf(ticker, base, price)
    except Exception:  # noqa: BLE001
        out["reverse_dcf"] = {"available": False}
    # Implied margin (freeze growth, solve for the operating margin the price needs).
    try:
        peer_max = _peer_max_ebit_margin(comps_data)
        if price:
            out["reverse_dcf_margin"] = valuation.reverse_dcf_margin(ticker, base, price, peer_max_margin_pct=peer_max)
            out["current_ebit_margin_pct"] = base.get("ebit_margin_pct")
            out["peer_max_ebit_margin_pct"] = peer_max
    except Exception:  # noqa: BLE001
        out["reverse_dcf_margin"] = {"available": False}
    return out


def _peer_max_ebit_margin(comps_data: dict[str, Any]) -> float | None:
    """Best-in-class EBIT margin (%) across the GICS peer set, for the perfection check."""
    peers = comps_data.get("sector_peers") or []
    vals = []
    for r in peers:
        m = r.get("ebit_margin")
        if isinstance(m, (int, float)):
            vals.append(m * 100.0 if abs(m) <= 1.5 else m)  # stored as fraction
    return round(max(vals), 1) if vals else None


def _base_assumptions(packet, hist, w, ttm: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = _latest_facts(packet)
    rev = facts.get("revenue")
    ebit = facts.get("earnings_before_interest_taxes")
    ebit_margin = (ebit / rev * 100.0) if (ebit and rev) else 40.0
    # near-term growth ~ recent revenue CAGR from history, capped to a sane band
    g = 8.0
    revs = [r.get("revenue") for r in (hist or []) if r.get("revenue")]
    if len(revs) >= 2 and revs[0]:
        yrs = len(revs) - 1
        g = max(2.0, min(20.0, ((revs[-1] / revs[0]) ** (1 / yrs) - 1) * 100.0))
    capex_pct = 20.0
    if hist and hist[-1].get("capex_pct_revenue"):
        capex_pct = hist[-1]["capex_pct_revenue"]

    # Anchor the base year to TTM when available: the trailing-twelve-months EBIT
    # margin is a fresher starting point than the last fiscal year, and the latest
    # TTM YoY revenue growth tempers the historical CAGR toward current momentum.
    if ttm:
        if ttm.get("ebit_margin_pct") is not None:
            ebit_margin = float(ttm["ebit_margin_pct"])
        ttm_g = None
        if ttm.get("revenue") and rev:
            ttm_g = (float(ttm["revenue"]) / float(rev) - 1.0) * 100.0
        if ttm_g is not None:
            g = max(2.0, min(20.0, 0.5 * g + 0.5 * ttm_g))
    return {
        "rev_growth_pct": [round(g, 1)] * DCF_HORIZON_YEARS,
        "ebit_margin_pct": round(ebit_margin, 1),
        "tax_rate_pct": w.get("tax_rate_pct", 21.0),
        "capex_pct_of_rev": round(capex_pct, 1),
        "nwc_pct_of_rev": 2.0,
        "wacc_pct": w.get("wacc_pct", 9.0),
        "terminal_growth_pct": 2.5,
    }


def _latest_facts(packet) -> dict[str, float]:
    rows = (packet.get("modeled_statements") or {}).get("rows") or []
    best: dict[str, tuple[int, float]] = {}
    for r in rows:
        li, v, fy = r.get("line_item_id"), r.get("value"), r.get("fiscal_year") or 0
        if li is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if li not in best or fy > best[li][0]:
            best[li] = (fy, v)
    return {k: v for k, (_, v) in best.items()}


def _stock_price(packet) -> float | None:
    for r in (packet.get("market_metrics") or {}).get("rows") or []:
        if r.get("metric_id") == "stock_price" and r.get("value"):
            try:
                return float(r["value"])
            except (TypeError, ValueError):
                pass
    return None


# ------------------------------------------------- Nodes: macro/news + 13F

def news_macro_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    cfg = get_config(state)
    if not cfg.get("enable_news", True):
        return {"macro": {"available": False}, "news": {"available": False}}
    try:
        macro = newsmacro.macro_context()
    except Exception as exc:  # noqa: BLE001
        macro = {"available": False, "note": str(exc)[:200]}
    try:
        news = newsmacro.news_summary(state["ticker"])
    except Exception as exc:  # noqa: BLE001
        news = {"available": False, "note": str(exc)[:200]}
    return {"macro": macro, "news": news}


def institutional_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    if state.get("jurisdiction") == "INTL":
        return {"ownership": {"available": False, "note": "INTL — 13F is US-only."}}
    if not get_config(state).get("enable_13f", True):
        return {"ownership": {"available": False}}
    try:
        return {"ownership": institutional.ownership_summary(state["ticker"])}
    except Exception as exc:  # noqa: BLE001
        return {"ownership": {"available": False, "note": str(exc)[:200]}}


# ------------------------------------------------ Node: qlib quant signals (alpha + risk)

def _quant_peer_tickers(state: InvestmentCommitteeState, limit: int = 15) -> list[str]:
    """Peer tickers from the comps block, for the factor-risk / optimizer peer group."""
    comps = ((state.get("analytics") or {}).get("comps") or {})
    peers: list[str] = []
    for p in (comps.get("sector_peers") or []):
        t = p.get("ticker") or p.get("symbol") if isinstance(p, dict) else None
        if t and str(t) not in peers:
            peers.append(str(t))
    return peers[:limit]


def qlib_signals_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    """Prepare-phase evidence: qlib alpha (expected return) + factor-structured risk.

    Shared across providers (prepare phase). Degrades to ``available: False`` on any
    missing model / dependency / error so it can never sink a committee run.
    """
    if not get_config(state).get("enable_quant_signals", True):
        return {"quant_signals": {"available": False, "note": "disabled by config"}}

    ticker = state.get("ticker")
    jurisdiction = state.get("jurisdiction") or "US"
    try:
        from api.quant import alpha_signal, qlib_optimize, qlib_risk
    except Exception as exc:  # noqa: BLE001 - qlib not installed
        return {"quant_signals": {"available": False, "note": f"qlib unavailable: {str(exc)[:150]}"}}

    out: dict[str, Any] = {"available": True, "ticker": ticker, "jurisdiction": jurisdiction}

    # --- learned alpha (expected return) ---
    try:
        meta = alpha_signal.model_meta(jurisdiction)
        cross = alpha_signal.latest_cross_section(jurisdiction)
        if meta is None or cross.empty:
            out["alpha"] = {"available": False, "note": "no trained alpha model for this jurisdiction"}
        else:
            er = float(cross[ticker]) if ticker in cross.index else None
            pct = round(100.0 * float((cross <= er).mean()), 1) if er is not None else None
            out["alpha"] = {
                "available": er is not None,
                "expected_return_monthly": er,
                "expected_return_annual": (er * meta["annualization"]) if er is not None else None,
                "universe_percentile": pct,
                "model_rank_ic": (meta.get("metrics") or {}).get("rank_ic_mean"),
                "horizon_months": meta.get("horizon_months"),
                "trained_at": meta.get("trained_at"),
            }
    except Exception as exc:  # noqa: BLE001
        out["alpha"] = {"available": False, "note": str(exc)[:150]}

    # --- factor-structured forward risk + optimizer weight over the peer group ---
    try:
        from datetime import date, timedelta

        import numpy as np

        peers = _quant_peer_tickers(state)
        universe = [ticker] + [p for p in peers if p != ticker]
        start = date.today() - timedelta(days=730)
        present, R, _dates = qlib_risk.load_price_returns(universe, jurisdiction, start, date.today())
        if ticker in present and R.shape[1] >= 3 and R.shape[0] >= 60:
            k = min(5, R.shape[1] - 1)
            sr = qlib_risk.structured_cov(present, R, num_factors=k)
            i = present.index(ticker)
            er_map = alpha_signal.expected_returns(jurisdiction, present)
            ann = (alpha_signal.model_meta(jurisdiction) or {}).get("annualization", 12.0)
            mu = np.array([(er_map.get(t) or 0.0) * ann for t in present])
            sol = qlib_optimize.solve("qlib_gmv", present, mu, sr.sigma)
            out["risk"] = {
                "available": True,
                "peer_group_size": len(present),
                "forward_vol_annual": round(float(sr.vol[i]), 4),
                "min_variance_weight": round(float(sol.weights[i]), 4),
                "factor_exposures": {
                    sr.factor_names[j]: round(float(sr.factor_exposure[i, j]), 3)
                    for j in range(min(3, sr.factor_exposure.shape[1]))
                },
            }
        else:
            out["risk"] = {"available": False, "note": "insufficient peer price history for a factor risk model"}
    except Exception as exc:  # noqa: BLE001
        out["risk"] = {"available": False, "note": str(exc)[:150]}

    return {"quant_signals": out}


# ------------------------------------------------ Node: per-ticker DQ + mapping agent

_DQ_ESCALATE_SEVERITIES = {"medium", "high", "blocker"}


def _dq_should_escalate(report: dict[str, Any] | None, mapping_pack: dict[str, Any]) -> bool:
    """Cost gate: only spend a DeepSeek call when findings actually warrant triage."""
    if not mapping_pack.get("is_empty", True):
        return True
    report = report or {}
    for finding in report.get("findings") or []:
        if isinstance(finding, dict) and str(finding.get("severity") or "").lower() in _DQ_ESCALATE_SEVERITIES:
            return True
    for score in (report.get("layer_scores") or {}).values():
        try:
            if float(score) < 85.0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _dq_triage_model(state: InvestmentCommitteeState) -> str:
    """Chat-tier model for triage, resolved against the run's provider.

    The old guard here forced a ``deepseek-`` prefix because chat_json used to
    reject anything else; the runtime is provider-aware now, so the only job left
    is to fall back to the provider's chat model when nothing is configured.
    """
    model = get_config(state).get("dq_agent_model")
    if model and not (_provider(state) != llm_providers.DEFAULT_PROVIDER
                      and model in _LEGACY_DEEPSEEK_MODELS):
        return llm_providers.chat_model(_provider(state), model)
    return _structured_model(state)


def data_quality_agent_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    """Runs in parallel with news_macro/institutional after the engine.

    Reuses the deterministic report the engine already built, adds a per-ticker mapping
    evidence pack, triages with DeepSeek behind a cost gate, and writes mapping proposals
    to the review queue. Degrades to the deterministic report when the LLM is unavailable
    or fails — it never gates the run.
    """
    if state.get("jurisdiction") == "INTL":
        return {"data_quality_agent": {"available": False, "note": "INTL — no XBRL mapping layer."}}
    cfg = get_config(state)
    if not cfg.get("enable_data_quality_agent", True):
        return {"data_quality_agent": {"available": False, "note": "disabled"}}

    ticker = state["ticker"]
    jurisdiction = state.get("jurisdiction") or "US"
    entity_id = state.get("cik") if jurisdiction == "US" else state.get("edinet_code")
    report = state.get("data_quality_report") or {}
    packet = state.get("financial_ratios") or {}
    try:
        sector_scope = data_quality_agent._sector_scope(packet)
    except Exception:  # noqa: BLE001
        sector_scope = "corp"

    out: dict[str, Any] = {"available": True, "sector_scope": sector_scope}

    # (1) per-ticker mapping evidence pack (advisory; failure is non-fatal)
    try:
        mapping_pack = dq_triage.build_mapping_pack(
            ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id, sector_scope=sector_scope
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq mapping pack failed for %s: %s", ticker, exc)
        mapping_pack = {"is_empty": True, "note": str(exc)[:200]}
    out["mapping_pack"] = mapping_pack

    # (2) escalation gate — LLM only when a key is present and findings warrant it
    key = _resolve_key(state)
    if not key:
        out["triage_skipped_reason"] = "llm_disabled"
    elif not (cfg.get("dq_agent_always", False) or _dq_should_escalate(report, mapping_pack)):
        out["triage_skipped_reason"] = "no_material_findings"
    else:
        try:
            triage = _run_dq_triage(state, ticker, jurisdiction, sector_scope, report, mapping_pack)
            out["triage"] = triage
            proposals = triage.get("proposals") or []
            # (3) write mapping proposals to the review queue (queue-only, never versioned)
            if cfg.get("dq_agent_queue_proposals", True) and proposals:
                try:
                    out["queued_proposal_ids"] = dq_triage.queue_proposals(
                        proposals, jurisdiction=jurisdiction, ticker=ticker, entity_id=entity_id
                    )
                except Exception as exc:  # noqa: BLE001 - queue write must not fail the run
                    logger.warning("dq queue write failed for %s: %s", ticker, exc)
                    out["queue_error"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - LLM failure degrades gracefully
            logger.warning("dq triage LLM failed for %s: %s", ticker, exc)
            out["triage_skipped_reason"] = f"llm_error: {type(exc).__name__}"

    # (4) phase-2 finding persistence + run-over-run deltas (advisory)
    try:
        explained = [
            item.get("finding_id")
            for item in ((out.get("triage") or {}).get("triage") or [])
            if isinstance(item, dict) and item.get("root_cause") == "benign_definition_difference"
        ]
        out["finding_deltas"] = dq_triage.record_findings(
            report, ticker=ticker, jurisdiction=jurisdiction, entity_id=entity_id,
            explained_ids=[fid for fid in explained if fid],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq finding persistence failed for %s: %s", ticker, exc)

    return {"data_quality_agent": out}


def _run_dq_triage(
    state: InvestmentCommitteeState,
    ticker: str,
    jurisdiction: str,
    sector_scope: str,
    report: dict[str, Any],
    mapping_pack: dict[str, Any],
) -> dict[str, Any]:
    # DeepSeek JSON mode (not tool-calling): with_structured_output returns empty tool
    # args for this nested schema, whereas json_object generation is reliable. We still
    # validate the result into committee.schemas.DqTriage via dq_triage.parse_triage.
    prompt = dq_triage.build_triage_prompt(
        ticker=ticker,
        jurisdiction=jurisdiction,
        sector_scope=sector_scope,
        report_compact=data_quality_agent.compact_data_quality_report(report),
        mapping_pack=mapping_pack,
    )
    raw = llm_runtime.chat_json(
        api_key=_resolve_key(state),
        provider=_provider(state),
        model=_dq_triage_model(state),
        system_prompt=dq_triage.TRIAGE_SYSTEM_PROMPT,
        user_prompt=prompt,
        temperature=0.1,
        max_tokens=3500,
    )
    return dq_triage.parse_triage(raw).model_dump(mode="json")


def _load_mda(cik: str | None, edinet_code: str | None, jurisdiction: str) -> str:
    try:
        if jurisdiction == "US":
            if not cik:
                return ""
            df = read_sql(
                """
                SELECT filing_id, section_id, form_type, filed_date, section_text
                FROM fact_mda_sections_us
                WHERE cik = %(eid)s AND section_id IN ('item_7','item_2')
                  AND section_text IS NOT NULL
                ORDER BY filed_date DESC NULLS LAST,
                         CASE section_id WHEN 'item_2' THEN 0 WHEN 'item_7' THEN 1 ELSE 2 END,
                         char_count DESC NULLS LAST
                LIMIT 3
                """,
                {"eid": str(cik).zfill(10)},
            )
        else:
            if not edinet_code:
                return ""
            df = read_sql(
                """
                SELECT filing_id, section_id, doc_type_code AS form_type, filed_date, section_text
                FROM fact_mda_sections_jp
                WHERE edinet_code = %(eid)s AND section_text IS NOT NULL
                ORDER BY filed_date DESC NULLS LAST, char_count DESC NULLS LAST
                LIMIT 3
                """,
                {"eid": edinet_code},
            )
        if df.empty:
            return ""
        return _format_recent_mda_rows(df.to_dict("records"), jurisdiction)[:_MDA_CAP]
    except Exception:  # noqa: BLE001
        return ""


def _load_rich_filing_sections(ticker: str, jurisdiction: str, target_years: list[int] | None) -> dict[str, Any]:
    if jurisdiction != "US":
        packet = {
            "available": False,
            "ticker": ticker,
            "sections": [],
            "warnings": ["rich filing sections are US-only in v1"],
        }
        packet["compact"] = rich_filing_sections.compact_rich_filing_sections(packet)
        return packet
    try:
        return rich_filing_sections.fetch_rich_filing_sections(
            ticker,
            years=target_years or None,
            limit_filings=2,
            max_sections=16,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rich filing section extraction failed for %s: %s", ticker, exc)
        return {
            "available": False,
            "ticker": ticker,
            "sections": [],
            "compact": {"available": False, "sections": [], "warnings": [str(exc)[:200]], "truncated": False},
            "warnings": [f"rich filing section extraction failed: {exc.__class__.__name__}: {str(exc)[:160]}"],
        }


def _format_recent_mda_rows(rows: list[dict[str, Any]], jurisdiction: str) -> str:
    if not rows:
        return ""
    weights = (0.55, 0.30, 0.15)
    active_rows = rows[:3]
    total_weight = sum(weights[: len(active_rows)]) or 1.0
    parts = []
    for index, row in enumerate(active_rows):
        text = str(row.get("section_text") or "").strip()
        if not text:
            continue
        weight = weights[index] if index < len(weights) else 0.05
        budget = max(500, int(_MDA_CAP * weight / total_weight))
        filed = row.get("filed_date")
        filed_text = filed.isoformat() if hasattr(filed, "isoformat") else str(filed or "undated")
        header = (
            f"[{filed_text} {row.get('form_type') or ''} {row.get('section_id') or ''}; "
            f"filing {row.get('filing_id') or ''}; recency_weight={weight:.0%}; "
            f"source=sec.fact_mda_sections_{jurisdiction.lower()}]"
        )
        parts.append(f"{header}\n{text[:budget].rstrip()}")
    return "\n\n".join(parts)[:_MDA_CAP]


# --------------------------------------------------------- Node 4: the tribunal

def advocate_analyst_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    return _run_agent(state, "advocate", prompts.ADVOCATE_PROMPT, "advocate_analysis")


def challenger_analyst_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    return _run_agent(state, "challenger", prompts.CHALLENGER_PROMPT, "challenger_analysis")


def auditor_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    return _run_agent(state, "auditor", prompts.AUDITOR_PROMPT, "auditor_analysis")


# --------------------------------------------------- user-defined extra analysts
# The tribunal roster is extensible: the UI (or any caller) may pass
# ``config["extra_analysts"] = [{"name": ..., "mandate": ...}, ...]``. Each entry
# becomes an additional agent node that debates alongside Advocate/Challenger/Auditor and,
# because it writes to ``committee_chat_history`` (the fan-in reducer), feeds the
# Lead synthesis automatically.

_RESERVED_ANALYST_KEYS = {"advocate", "challenger", "auditor", "base", "lead"}
_MAX_EXTRA_ANALYSTS = 10


def normalize_extra_analysts(raw: Any, *, max_count: int = _MAX_EXTRA_ANALYSTS) -> list[dict[str, Any]]:
    """Validate/clean caller-supplied analysts into ``[{key, name, mandate}]``.

    Drops malformed entries (missing name/mandate), slugifies a stable ``key`` per
    analyst, de-dupes, and avoids colliding with the built-in stances. Caps the
    count so a caller can't explode the graph / token budget. Built-in specialist
    archetypes may also pass a stable key plus metadata; user-added analysts only
    need the legacy name/mandate fields.
    """
    import re

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (raw or []):
        if len(out) >= max_count or not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        mandate = str(item.get("mandate") or item.get("prompt") or "").strip()
        if not name or not mandate:
            continue
        raw_key = str(item.get("key") or "").strip()
        base_src = raw_key or name
        base = re.sub(r"[^a-z0-9]+", "_", base_src.lower()).strip("_") or f"analyst_{len(out) + 1}"
        key = base
        i = 2
        while key in seen or key in _RESERVED_ANALYST_KEYS:
            key = f"{base}_{i}"
            i += 1
        seen.add(key)
        out.append({
            "key": key,
            "name": name[:80],
            "mandate": mandate[:2600],
            "origin": str(item.get("origin") or "custom")[:40],
            "focus": str(item.get("focus") or "")[:120],
            "emit_structured": bool(item.get("emit_structured") or item.get("structured")),
        })
    return out


def default_specialist_analysts(company: dict[str, Any] | None,
                                config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Sector-aware built-in specialists, normalized for graph node creation."""
    return normalize_extra_analysts(archetypes.specialist_roster(company, config), max_count=_MAX_EXTRA_ANALYSTS)


def merge_analyst_rosters(*rosters: Any) -> list[dict[str, Any]]:
    """Merge specialist and caller-provided analyst rosters without duplicate keys."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for roster in rosters:
        for analyst in normalize_extra_analysts(roster, max_count=_MAX_EXTRA_ANALYSTS):
            key = analyst["key"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(analyst)
            if len(merged) >= _MAX_EXTRA_ANALYSTS:
                return merged
    return merged


def _extra_persona_prompt(name: str, mandate: str) -> str:
    return (
        f"You are {name.upper()} — a specialist analyst the portfolio manager has added to this committee.\n"
        f"YOUR MANDATE / LENS: {mandate.strip()}\n\n"
        "Argue your case from that mandate with the same rigor as the other analysts — quantitative, "
        "grounded strictly in the supplied evidence, and reconciled against the triangulation. Do not "
        "restate another analyst's role; bring the distinct perspective your mandate demands. When your "
        "lens implies a DCF, sensitivity, WACC, or peer-multiple adjustment, state it explicitly."
        + prompts._COMMON
    )


def make_extra_analyst_node(key: str, name: str, mandate: str, *, emit_structured: bool = False):
    """Build a committee node for a user-defined analyst, reusing ``_run_agent``."""
    system_prompt = _extra_persona_prompt(name, mandate)
    slot = f"{key}_analysis"

    def _node(state: InvestmentCommitteeState) -> dict[str, Any]:
        return _run_agent(state, key, system_prompt, slot, analyst_name=name, emit_structured=emit_structured)

    _node.__name__ = f"{key}_analyst_node"
    return _node


def _run_agent(state: InvestmentCommitteeState, stance: str, system_prompt: str, slot: str, *,
               analyst_name: str | None = None, emit_structured: bool = False) -> dict[str, Any]:
    if not _resolve_key(state):
        text = _fallback_agent_text(state, stance)
        return {slot: text, "committee_chat_history": [{"role": stance, "content": text}]}
    # Deep reasoning (deepseek-reasoner), plain text — the reasoner does not do
    # reliable structured output, and the richer chain-of-thought is the point.
    payload = _agent_payload(state)
    prompt = (
        system_prompt
        + "\n\nEVIDENCE (JSON — WACC, segments, cash-flow history, incremental ROIC, reverse-DCF, "
          "comps, macro regime, 13F ownership):\n" + json.dumps(payload, default=str)[:26000]
        + f"\n\nWrite the {stance.upper()} case now, following the output format above. Be quantitative "
          "and cite the numbers from the evidence. Plain text, no JSON."
    )
    try:
        text = _reason(state, prompt, max_tokens=4000, temperature=0.35)
    except Exception as exc:  # noqa: BLE001 - a failed agent must not sink the debate
        text = _fallback_agent_text(state, stance) + f"\n(LLM error: {exc.__class__.__name__})"
    out: dict[str, Any] = {slot: text, "committee_chat_history": [{"role": stance, "content": text}]}
    if emit_structured and get_config(state).get("enable_specialist_structured_outputs", True):
        verdict = _specialist_structured_verdict(state, stance, analyst_name or stance, text)
        if verdict:
            out["specialist_verdicts"] = [verdict]
    return out


def _specialist_structured_verdict(state: InvestmentCommitteeState, key: str, name: str,
                                   analyst_text: str) -> dict[str, Any] | None:
    """Second-tier structured extraction for specialist analyst signals."""
    payload = _agent_payload(state)
    prompt = (
        prompts.SPECIALIST_STRUCTURED_PROMPT
        + f"\n\nANALYST KEY: {key}\nANALYST NAME: {name}\n"
        + "\nEVIDENCE (JSON):\n" + json.dumps(payload, default=str)[:16000]
        + "\n\nANALYST PROSE:\n" + analyst_text[:6000]
        + "\n\nReturn a SpecialistVerdict. Use nulls when a number is not in the evidence."
    )
    try:
        result = _invoke_structured(state, SpecialistVerdict, prompt, temperature=0.1, max_tokens=1600, attempts=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("specialist structured verdict failed (%s): %s", key, exc)
        return None
    if hasattr(result, "model_dump"):
        data = result.model_dump()
    else:  # pydantic v1
        data = result.dict()
    data["analyst_key"] = key
    data["analyst"] = name
    return data


def _format_thesis(t: AgentThesis) -> str:
    lines = [t.thesis.strip()]
    if t.key_claims:
        lines.append("Claims: " + "; ".join(t.key_claims))
    if t.segment_read:
        lines.append("Segments: " + t.segment_read)
    if t.falsification_kpis:
        lines.append("Falsifies if: " + "; ".join(t.falsification_kpis))
    tilt = []
    if t.rev_growth_tilt_pct is not None: tilt.append(f"rev_growth={t.rev_growth_tilt_pct}%")
    if t.ebit_margin_pct is not None: tilt.append(f"ebit_margin={t.ebit_margin_pct}%")
    if t.wacc_pct is not None: tilt.append(f"wacc={t.wacc_pct}%")
    if t.terminal_growth_pct is not None: tilt.append(f"g={t.terminal_growth_pct}%")
    if tilt:
        lines.append("DCF tilt: " + ", ".join(tilt) + f" (confidence {t.confidence:.2f})")
    return "\n".join(lines)


# ----------------------------------------------------------------- Node 5: lead

def lead_analyst_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    ticker = state["ticker"]
    cfg = get_config(state)
    iteration = int(state.get("iteration_count") or 0) + 1
    analytics = state.get("analytics") or {}

    if _resolve_key(state):
        try:
            verdict = _lead_verdict(state)
            scenario_dicts = [_scenario_dict(s) for s in verdict.scenarios]
            synthesis = verdict.synthesis
            decision_ready = verdict.decision_ready
        except Exception as exc:  # noqa: BLE001
            scenario_dicts = _fallback_scenarios(state)
            synthesis = f"Deterministic fallback synthesis (LLM error: {exc.__class__.__name__})."
            decision_ready = True
    else:
        scenario_dicts = _fallback_scenarios(state)
        synthesis = f"Deterministic fallback synthesis (no working {_provider(state)} key)."
        decision_ready = True

    # Weights: base from config, dynamically tilted by the macro/news signal.
    weights = _macro_adjusted_weights(cfg, state.get("macro") or {})
    for s in scenario_dicts:
        s["weight"] = weights.get(s.get("label"), s.get("weight", 0))
    scenario_dicts = _normalize_weights(scenario_dicts)

    # Consolidated DCF (cross-check).
    dcf_result = valuation.run_scenarios(ticker, scenario_dicts)

    # SOTP (primary) — per-segment growth/margin/capex/WACC, scenario-weighted.
    seg_rows = (state.get("segment_data") or {}).get("structured") or []
    w = analytics.get("wacc") or {"wacc_pct": 9.0}
    shares = analytics.get("shares") or 0
    net_debt = analytics.get("net_debt") or 0
    base_growth = (sum((analytics.get("base_assumptions") or {}).get("rev_growth_pct", [8])) /
                   max(1, len((analytics.get("base_assumptions") or {}).get("rev_growth_pct", [8]))))
    sotp = {"available": False}
    if cfg.get("enable_sotp", True) and seg_rows and shares:
        try:
            sotp = valuation.sotp_scenarios(seg_rows, w, net_debt=net_debt, shares=shares,
                                            base_growth=base_growth, weights=weights)
        except Exception as exc:  # noqa: BLE001
            sotp = {"available": False, "note": str(exc)[:200]}

    price = analytics.get("current_price")
    comps_implied = ((analytics.get("comps") or {}).get("implied")) or {}
    triangulation = valuation.triangulate(sotp, dcf_result, comps_implied, price, shares)

    # Full projected income-statement model per scenario (foots to the per-share above).
    dcf_models = _build_dcf_models(state, dcf_result, w, shares)

    return {
        "lead_synthesis": synthesis,
        "scenarios": dcf_result,
        "sotp": sotp,
        "triangulation": triangulation,
        "reverse_dcf": analytics.get("reverse_dcf") or {"available": False},
        "primary_fair_value": triangulation.get("primary_fair_value"),
        "probability_weighted_fair_value": dcf_result.get("probability_weighted_fair_value"),
        "dcf_model": dcf_models.get("base"),
        "dcf_models": dcf_models,
        "decision_ready": bool(decision_ready) or iteration >= cfg.get("max_iterations", _MAX_ITERATIONS),
        "iteration_count": iteration,
    }


def _build_dcf_models(state: InvestmentCommitteeState, dcf_result: dict[str, Any],
                      w: dict[str, Any], shares: float) -> dict[str, Any]:
    facts = _latest_facts(state.get("financial_ratios") or {})
    rev, gp = facts.get("revenue"), facts.get("gross_profit")
    gm_pct = (gp / rev * 100.0) if (gp and rev) else 65.0
    out: dict[str, Any] = {}
    for r in dcf_result.get("scenarios") or []:
        full = r.get("dcf_full")
        if not full or not full.get("projected_income"):
            continue
        try:
            out[r["label"]] = model_mod.build_income_statement_model(
                full, gross_margin_pct=gm_pct, interest_expense=w.get("interest_expense"),
                debt=w.get("total_debt"), cost_of_debt_pct=w.get("cost_of_debt_pct"),
                tax_rate_pct=(full.get("assumptions") or {}).get("tax_rate_pct", 21.0),
                shares=shares, base_revenue=rev)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dcf model build failed (%s): %s", r.get("label"), exc)
    return out


def _macro_adjusted_weights(cfg: dict[str, Any], macro: dict[str, Any]) -> dict[str, float]:
    base = dict(cfg.get("base_weights") or {"upside": 0.25, "base": 0.50, "downside": 0.25})
    if cfg.get("scenario_weight_mode") != "macro_adjusted":
        return base
    tilt = ((macro.get("signal") or {}).get("tilt")) if macro else None
    shift = 0.08
    if tilt == "supportive":
        base["upside"] = base.get("upside", 0.25) + shift
        base["downside"] = max(0.05, base.get("downside", 0.25) - shift)
    elif tilt == "cautious":
        base["downside"] = base.get("downside", 0.25) + shift
        base["upside"] = max(0.05, base.get("upside", 0.25) - shift)
    total = sum(base.values()) or 1.0
    return {k: v / total for k, v in base.items()}


def _lead_verdict(state: InvestmentCommitteeState) -> CommitteeVerdict:
    payload = _agent_payload(state)
    extra_block = _format_extra_analyst_block(state)
    structured_block = ""
    if state.get("specialist_verdicts"):
        structured_block = (
            "\n\nSPECIALIST STRUCTURED SIGNALS (JSON):\n"
            + json.dumps(_latest_specialist_verdicts(state), default=str)[:10000]
        )
    prompt = (
        prompts.LEAD_PROMPT
        + "\n\nDATA PACKET (JSON):\n" + json.dumps(payload, default=str)[:20000]
        + "\n\nADVOCATE:\n" + (state.get("advocate_analysis") or "")
        + "\n\nCHALLENGER:\n" + (state.get("challenger_analysis") or "")
        + "\n\nAUDITOR:\n" + (state.get("auditor_analysis") or "")
        + extra_block
        + structured_block
        + "\n\nReturn a CommitteeVerdict with exactly three scenarios (upside, base, downside)."
    )
    return _invoke_structured(state, CommitteeVerdict, prompt, temperature=0.2, max_tokens=2000)


def _scenario_dict(s: ScenarioAssumptions) -> dict[str, Any]:
    return {
        "label": s.label,
        "rev_growth_pct": s.rev_growth_pct or None,
        "terminal_growth_pct": s.terminal_growth_pct,
        "ebit_margin_pct": s.ebit_margin_pct,
        "tax_rate_pct": s.tax_rate_pct,
        "capex_pct_of_rev": s.capex_pct_of_rev,
        "nwc_pct_of_rev": s.nwc_pct_of_rev,
        "wacc_pct": s.wacc_pct,
        "weight": s.weight,
        "rationale": s.rationale,
    }


# --------------------------------------------------------------- Node 6: memo

def memo_generator_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    if _resolve_key(state):
        try:
            memo = _memo_bilingual(state)
        except Exception as exc:  # noqa: BLE001
            memo = _fallback_memo(state)
            memo["en"] += f"\n\n_(LLM memo fallback: {exc.__class__.__name__})_"
    else:
        memo = _fallback_memo(state)
    _write_memo_files(state, memo)
    return {"memo": memo}


def _memo_bilingual(state: InvestmentCommitteeState) -> dict[str, str]:
    # Two plain-text calls (EN, DE) are far more robust than one large structured
    # bilingual call — DeepSeek's tool-calling truncates ~10k-char string fields.
    summary = _memo_payload(state)
    base = prompts.MEMO_ONE_LANG_PROMPT + "\n\nCOMMITTEE STATE (JSON):\n" + json.dumps(summary, default=str)[:20000]
    en = _plain_llm(state, base + "\n\nWrite the memo in ENGLISH. Markdown only.", max_tokens=3600)
    de = _plain_llm(
        state,
        base + "\n\nSchreibe das Memo auf DEUTSCH im Register der Finanzpresse (FT/Handelsblatt), "
               "mit Dezimalkomma. Nur Markdown, keine Vorrede.",
        max_tokens=3800,
    )
    return {"en": en, "de": de}


def _reason(state: InvestmentCommitteeState, prompt: str, *, max_tokens: int = 3200,
            attempts: int = 3, temperature: float = 0.3) -> str:
    """Plain-text call on the provider's deep-reasoning model."""
    from xbrl_sec.llm import make_reasoning_model, setup_llm_cache
    setup_llm_cache()
    model = _reasoning_model(state)
    last: Exception | None = None
    for i in range(attempts):
        try:
            llm = make_reasoning_model(_provider(state), model, temperature=temperature,
                                       max_tokens=max_tokens,
                                       api_key=state.get("api_key") or None)
            content = (getattr(llm.invoke(prompt), "content", "") or "").strip()
            if content:
                return content
            last = ValueError("empty content")
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("reasoner call attempt %d/%d failed (%s): %s", i + 1, attempts, model, exc)
        time.sleep(1.5 * (i + 1))
    raise last or RuntimeError("reasoner invoke failed")


# Backwards-compatible alias used by the memo node.
def _plain_llm(state: InvestmentCommitteeState, prompt: str, *, max_tokens: int, attempts: int = 3) -> str:
    return _reason(state, prompt, max_tokens=max_tokens, attempts=attempts, temperature=0.25)


# --------------------------------------------------------- error terminator

def error_terminator_node(state: InvestmentCommitteeState) -> dict[str, Any]:
    logger.warning(
        "[COMMITTEE CRITICAL] pipeline stopped for %s | complete=%s dq_passed=%s | dq_errors=%s",
        state.get("ticker"), state.get("is_data_complete"), state.get("is_dq_passed"),
        (state.get("dq_errors") or [])[:5],
    )
    return {}


# ----------------------------------------------------------------- payloads

def _agent_payload(state: InvestmentCommitteeState) -> dict[str, Any]:
    packet = state.get("financial_ratios") or {}
    seg = state.get("segment_data") or {}
    a = state.get("analytics") or {}
    comps_data = a.get("comps") or {}
    evidence_bundle = evidence_mod.merge_runtime_context(
        state.get("evidence_bundle"),
        ticker=state.get("ticker"),
        jurisdiction=state.get("jurisdiction"),
        macro=state.get("macro"),
        news=state.get("news"),
        ownership=state.get("ownership"),
    )
    return {
        # AUTHORITATIVE figures — every analyst must cite these verbatim (see prompts._COMMON).
        "canonical_metrics": metrics_mod.canonical(state),
        "evidence_bundle_compact": evidence_mod.compact_evidence_bundle(evidence_bundle),
        "rich_filing_sections_compact": state.get("rich_filing_sections_compact")
        or rich_filing_sections.compact_rich_filing_sections(state.get("rich_filing_sections")),
        "data_quality_report_compact": data_quality_agent.compact_data_quality_report(state.get("data_quality_report")),
        "data_quality_triage": dq_triage.compact_triage(state.get("data_quality_agent")),
        "company": packet.get("company"),
        "metrics": (packet.get("metrics") or {}).get("rows", [])[:80],
        "yahoo_cross_check": _yahoo_cross_check_brief(packet.get("yahoo_cross_check") or {}),
        "yahoo_latest_annual": (packet.get("yahoo_fundamentals") or {}).get("latest_annual"),
        "recon_flags": (packet.get("recon_flags") or {}).get("rows", [])[:12],
        "mda_excerpt": (state.get("mda_text") or "")[:_MDA_CAP],
        "segment_data": {
            "available": seg.get("available"),
            "structured": seg.get("structured", [])[:20],
            "narrative": (seg.get("narrative") or "")[:3000],
        },
        "segment_trend": a.get("segment_trend"),
        "wacc": _wacc_brief(a.get("wacc") or {}),
        "cashflow_history": a.get("cashflow_history"),
        "quarterly_trend": _quarterly_brief(a.get("quarterly") or {}),
        "incremental_roic": a.get("incremental_roic"),
        "reverse_dcf": a.get("reverse_dcf"),
        "reverse_dcf_margin": a.get("reverse_dcf_margin"),
        "current_price": a.get("current_price"),
        "comps": {
            "target": comps_data.get("target"),
            "sector_peers": (comps_data.get("sector_peers") or [])[:10],
            "peer_median": comps_data.get("peer_median"),
            "selection_rule": comps_data.get("selection_rule"),
            "implied": comps_data.get("implied"),
        },
        "macro": (state.get("macro") or {}).get("signal"),
        "macro_regime": (state.get("macro") or {}).get("regime"),
        "news": state.get("news"),
        "ownership": _ownership_brief(state.get("ownership") or {}),
        "quant_signals": state.get("quant_signals"),
    }


def _quarterly_brief(q: dict[str, Any]) -> dict[str, Any]:
    """Compact quarterly momentum for the agents: last few quarters + TTM."""
    if not q.get("available"):
        return {"available": False}
    keep = ("period_end", "fiscal_year", "fiscal_period", "revenue",
            "ebit_margin_pct", "fcf_margin_pct", "yoy_rev_growth_pct")
    return {
        "available": True,
        "latest_quarter_end": q.get("latest_quarter_end"),
        "quarters": [{k: qq.get(k) for k in keep} for qq in (q.get("quarters") or [])[-8:]],
        "ttm": q.get("ttm"),
    }


def _yahoo_cross_check_brief(check: dict[str, Any]) -> dict[str, Any]:
    if not check.get("available"):
        return {
            "available": False,
            "source_table": check.get("source_table"),
            "note": check.get("note") or check.get("summary"),
        }
    severity_rank = {"material": 0, "currency_mismatch": 1, "watch": 2, "informational": 3, "ok": 4}
    rows = sorted(
        check.get("rows") or [],
        key=lambda row: (severity_rank.get(str(row.get("severity")), 9), str(row.get("line_item_id") or "")),
    )
    keep = (
        "line_item_id", "label", "standardized_fiscal_year", "standardized_value",
        "standardized_currency", "yahoo_fiscal_year", "yahoo_metric_id",
        "yahoo_source_metric_key", "yahoo_value", "yahoo_currency", "pct_delta",
        "severity", "currency_mismatch", "comparison_basis",
    )
    return {
        "available": True,
        "source_table": check.get("source_table"),
        "basis": check.get("basis"),
        "snapshot_date": check.get("snapshot_date"),
        "summary": check.get("summary"),
        "material_count": check.get("material_count"),
        "watch_count": check.get("watch_count"),
        "rows": [{k: row.get(k) for k in keep} for row in rows[:12]],
    }


def _wacc_brief(w: dict[str, Any]) -> dict[str, Any]:
    keys = ("wacc_pct", "cost_of_equity_capm_pct", "cost_of_debt_pct", "risk_free_pct",
            "equity_risk_premium_pct", "credit_spread_pct", "interest_coverage", "tax_rate_pct")
    return {k: w.get(k) for k in keys if k in w}


def _ownership_brief(o: dict[str, Any]) -> dict[str, Any]:
    if not o.get("available"):
        return {"available": False}
    return {"available": True, "quarter": o.get("quarter"), "net_direction": o.get("net_direction"),
            "passive_share_of_reported_pct": o.get("passive_share_of_reported_pct"),
            "notable_adds": o.get("notable_adds"), "notable_reduces": o.get("notable_reduces")}


def _memo_payload(state: InvestmentCommitteeState) -> dict[str, Any]:
    packet = state.get("financial_ratios") or {}
    a = state.get("analytics") or {}
    comps_data = a.get("comps") or {}
    evidence_bundle = evidence_mod.merge_runtime_context(
        state.get("evidence_bundle"),
        ticker=state.get("ticker"),
        jurisdiction=state.get("jurisdiction"),
        macro=state.get("macro"),
        news=state.get("news"),
        ownership=state.get("ownership"),
    )
    return {
        # AUTHORITATIVE figures — the memo must cite these verbatim (see prompts._COMMON).
        "canonical_metrics": metrics_mod.canonical(state),
        "evidence_bundle_compact": evidence_mod.compact_evidence_bundle(evidence_bundle),
        "rich_filing_sections_compact": state.get("rich_filing_sections_compact")
        or rich_filing_sections.compact_rich_filing_sections(state.get("rich_filing_sections")),
        "data_quality_report_compact": data_quality_agent.compact_data_quality_report(state.get("data_quality_report")),
        "data_quality_triage": dq_triage.compact_triage(state.get("data_quality_agent")),
        "company": packet.get("company"),
        "synthesis": state.get("lead_synthesis"),
        "triangulation": state.get("triangulation"),
        "primary_fair_value": state.get("primary_fair_value"),
        "sotp": _sotp_brief(state.get("sotp") or {}),
        "consolidated_dcf": state.get("scenarios"),
        "dcf_model_base": model_mod.compact_summary(state.get("dcf_model") or {}),
        "reverse_dcf": state.get("reverse_dcf"),
        "reverse_dcf_margin": a.get("reverse_dcf_margin"),
        "wacc": _wacc_brief(a.get("wacc") or {}),
        "comps_target": comps_data.get("target"),
        "sector_peers": (comps_data.get("sector_peers") or [])[:10],
        "peer_median": comps_data.get("peer_median"),
        "peer_selection_rule": comps_data.get("selection_rule"),
        "yahoo_cross_check": _yahoo_cross_check_brief(packet.get("yahoo_cross_check") or {}),
        "yahoo_latest_annual": (packet.get("yahoo_fundamentals") or {}).get("latest_annual"),
        "cashflow_history": a.get("cashflow_history"),
        "quarterly_trend": _quarterly_brief(a.get("quarterly") or {}),
        "incremental_roic": a.get("incremental_roic"),
        "segment_data": {"structured": (state.get("segment_data") or {}).get("structured"),
                         "narrative": ((state.get("segment_data") or {}).get("narrative") or "")[:2500]},
        "segment_trend": a.get("segment_trend"),
        "macro": (state.get("macro") or {}).get("signal"),
        "macro_regime": (state.get("macro") or {}).get("regime"),
        "news": state.get("news"),
        "ownership": _ownership_brief(state.get("ownership") or {}),
        "advocate": state.get("advocate_analysis"),
        "challenger": state.get("challenger_analysis"),
        "auditor": state.get("auditor_analysis"),
        "extra_analysts": _extra_analyst_theses(state),
        "specialist_verdicts": _latest_specialist_verdicts(state),
    }


def _extra_analyst_theses(state: InvestmentCommitteeState) -> list[dict[str, str]]:
    """Theses from user-added analysts (any committee_chat_history role beyond the
    three built-ins), so the Lead/memo can weigh them too."""
    builtin = {"advocate", "challenger", "auditor"}
    latest: dict[str, str] = {}
    order: list[str] = []
    for h in (state.get("committee_chat_history") or []):
        role = str(h.get("role") or "")
        if role and role not in builtin:
            if role not in latest:
                order.append(role)
            latest[role] = (h.get("content") or "")[:1800]
    return [{"analyst": role, "thesis": latest[role]} for role in order if latest.get(role)]


def _format_extra_analyst_block(state: InvestmentCommitteeState) -> str:
    extras = _extra_analyst_theses(state)
    if not extras:
        return ""
    parts = []
    for item in extras:
        label = str(item.get("analyst") or "").upper()
        thesis = item.get("thesis") or ""
        parts.append(f"{label}:\n{thesis}")
    return "\n\nSPECIALIST / EXTRA ANALYSTS:\n" + "\n\n".join(parts)


def _latest_specialist_verdicts(state: InvestmentCommitteeState) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for verdict in (state.get("specialist_verdicts") or []):
        if not isinstance(verdict, dict):
            continue
        key = str(verdict.get("analyst_key") or verdict.get("analyst") or "")
        if not key:
            continue
        if key not in latest:
            order.append(key)
        latest[key] = verdict
    return [latest[key] for key in order if key in latest]


def _sotp_brief(sotp: dict[str, Any]) -> dict[str, Any]:
    if not sotp.get("available"):
        return {"available": False}
    return {"available": True, "per_share": sotp.get("per_share"), "weights": sotp.get("weights"),
            "weighted_per_share": sotp.get("weighted_per_share"),
            "segments_base": sotp.get("segments_base")}


def _dcf_brief(dcf: dict[str, Any]) -> dict[str, Any]:
    return {
        "implemented": dcf.get("implemented"),
        "per_share_value": dcf.get("per_share_value"),
        "current_price": dcf.get("current_price"),
        "upside_pct": dcf.get("upside_pct"),
        "assumptions": dcf.get("assumptions"),
        "historicals_used": dcf.get("historicals_used"),
        "message": dcf.get("message"),
    }


# ----------------------------------------------------------------- fallbacks

def _fallback_agent_text(state: InvestmentCommitteeState, stance: str) -> str:
    seg = state.get("segment_data") or {}
    n_seg = len(seg.get("structured") or [])
    # Name the provider that was actually asked: with several providers debating in
    # parallel, a hardcoded "no DeepSeek key" points at the wrong one.
    return (
        f"[{stance.upper()} — deterministic placeholder; no working {_provider(state)} key]\n"
        f"Argues the {stance} case from the packet; {n_seg} reportable segment rows available."
    )


def _fallback_scenarios(state: InvestmentCommitteeState) -> list[dict[str, Any]]:
    dcf = (state.get("financial_ratios") or {}).get("dcf") or {}
    a = dcf.get("assumptions") or {}
    base_g = (a.get("rev_growth_pct") or [4.0, 4.0, 4.0, 4.0, 4.0])
    base_margin = a.get("ebit_margin_pct", 15.0)
    base_wacc = a.get("wacc_pct", 9.0)
    g_base = sum(base_g) / len(base_g) if base_g else 4.0

    def sc(label, dg, dm, dw, weight):
        return {
            "label": label,
            "rev_growth_pct": [round(g_base + dg, 2)] * DCF_HORIZON_YEARS,
            "terminal_growth_pct": a.get("terminal_growth_pct", 2.5),
            "ebit_margin_pct": round(base_margin + dm, 2),
            "tax_rate_pct": a.get("tax_rate_pct", 21.0),
            "capex_pct_of_rev": a.get("capex_pct_of_rev", 4.0),
            "nwc_pct_of_rev": a.get("nwc_pct_of_rev", 2.0),
            "wacc_pct": round(base_wacc + dw, 2),
            "weight": weight,
            "rationale": f"Deterministic {label} tilt off the baseline DCF assumptions.",
        }

    return [
        sc("upside", +2.0, +2.0, -0.5, 0.20),
        sc("base", 0.0, 0.0, 0.0, 0.60),
        sc("downside", -1.0, -1.0, +0.25, 0.20),
    ]


def _normalize_weights(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = sum(float(s.get("weight", 0) or 0) for s in scenarios)
    if total <= 0:
        for s in scenarios:
            s["weight"] = 1.0 / len(scenarios)
    elif abs(total - 1.0) > 1e-6:
        for s in scenarios:
            s["weight"] = float(s.get("weight", 0) or 0) / total
    return scenarios


def _fallback_memo(state: InvestmentCommitteeState) -> dict[str, str]:
    company = (state.get("financial_ratios") or {}).get("company") or {}
    name = company.get("name") or state.get("ticker")
    tri = state.get("triangulation") or {}
    pwfv = state.get("primary_fair_value") or tri.get("primary_fair_value") or state.get("probability_weighted_fair_value")
    val = state.get("scenarios") or {}
    price = tri.get("current_price") or val.get("current_price")
    seg = state.get("segment_data") or {}

    def scen_table():
        out = []
        for s in (val.get("scenarios") or []):
            ps = s.get("per_share_value")
            out.append(f"- {s['label']}: {('%.2f' % ps) if isinstance(ps, (int, float)) else 'n/a'} "
                       f"(w={s.get('weight'):.0%})")
        return "\n".join(out) or "- (no scenario valuation available)"

    def seg_lines():
        rows = seg.get("structured") or []
        if not rows:
            return "Segment breakdown unavailable for this filer."
        return "\n".join(
            f"- {r.get('segment')}: revenue {r.get('revenue')}, op. margin "
            f"{('%.1f%%' % (r['operating_margin']*100)) if r.get('operating_margin') is not None else 'n/a'}"
            for r in rows[:10]
        )

    def specialist_lines():
        extras = _extra_analyst_theses(state)
        if not extras:
            return "- (no specialist analyst theses)"
        return "\n".join(f"- {x['analyst']}: {x['thesis'][:500]}" for x in extras)

    pwfv_s = ("%.2f" % pwfv) if isinstance(pwfv, (int, float)) else "n/a"
    price_s = ("%.2f" % price) if isinstance(price, (int, float)) else "n/a"
    en = (
        f"# Investment Committee Memo — {name}\n\n"
        f"**Probability-weighted fair value:** {pwfv_s} vs current price {price_s}.\n\n"
        f"## Scenarios\n{scen_table()}\n\n"
        f"## Segment breakdown\n{seg_lines()}\n\n"
        f"## Advocate\n{state.get('advocate_analysis','')}\n\n"
        f"## Challenger\n{state.get('challenger_analysis','')}\n\n"
        f"## Auditor\n{state.get('auditor_analysis','')}\n\n"
        f"## Specialists\n{specialist_lines()}\n\n"
        f"_Deterministic memo (no LLM). Synthesis: {state.get('lead_synthesis','')}_\n"
    )
    de = en.replace("Investment Committee Memo", "Investment-Komitee-Memo") \
           .replace("Probability-weighted fair value", "Wahrscheinlichkeitsgewichteter fairer Wert") \
           .replace("current price", "aktueller Kurs") \
           .replace("Scenarios", "Szenarien").replace("Segment breakdown", "Segmentaufschlüsselung")
    return {"en": en, "de": de}


def _write_memo_files(state: InvestmentCommitteeState, memo: dict[str, str]) -> None:
    """Persist all outputs inside the repo (``<repo>/output/committee/...``)."""
    try:
        from . import report_pdf
        out_dir = report_pdf.report_dir_for(str(state.get("ticker") or "UNKNOWN"))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "memo_en.md").write_text(memo.get("en", ""), encoding="utf-8")
        (out_dir / "memo_de.md").write_text(memo.get("de", ""), encoding="utf-8")
        (out_dir / "final_state.json").write_text(json.dumps(dict(state), indent=2, default=str), encoding="utf-8")
        # The branded story + appendix HTML/PDF (headless-Chrome render).
        result = report_pdf.write_report(dict(state), out_dir)
        logger.info("committee report written: %s", result.get("pdf") or result.get("html"))
    except Exception as exc:  # noqa: BLE001 - file output is best-effort
        logger.warning("committee report write failed: %s", exc)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default
