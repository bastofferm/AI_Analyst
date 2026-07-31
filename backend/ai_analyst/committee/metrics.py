"""Canonical market & valuation metrics — the single source of truth.

Every deterministic number the committee argues over already exists somewhere in
``state["analytics"]`` (the WACC block, the comps target row, the cash-flow
history, the reverse-DCF). The problem the report review surfaced is *drift*: the
reasoning LLM re-derives its own market cap / P/E / EV-EBITDA / ROIC and gets them
wrong, contradicting the correct tables.

This module flattens those already-correct figures into ONE labelled block that
(a) is injected at the top of the agent/memo payload with an instruction to cite
it verbatim, and (b) is rendered as an authoritative "market snapshot" card in the
report. It performs no new financial modelling — it only *selects* the canonical
value for each metric so every surface agrees.
"""
from __future__ import annotations

from typing import Any


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def canonical(state: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical metrics block, or ``{"available": False}`` if the
    deterministic analytics haven't been computed (e.g. gate failure)."""
    a = state.get("analytics") or {}
    w = a.get("wacc") or {}
    comps = a.get("comps") or {}
    target = comps.get("target") or {}
    hist = a.get("cashflow_history") or []
    last = hist[-1] if hist else {}
    inc = a.get("incremental_roic") or {}
    rdcf = state.get("reverse_dcf") or a.get("reverse_dcf") or {}
    rmargin = a.get("reverse_dcf_margin") or {}
    own = state.get("ownership") or {}

    price = _f(a.get("current_price"))
    shares = _f(a.get("shares"))
    market_cap = _f(w.get("market_cap")) or _f(target.get("market_cap"))
    if market_cap is None and price and shares:
        market_cap = price * shares
    net_debt = _f(a.get("net_debt")) or 0.0
    enterprise_value = (market_cap + net_debt) if market_cap is not None else None

    fcf = _f(last.get("free_cash_flow"))
    ocf = _f(last.get("operating_cash_flow"))
    capex = _f(last.get("capex"))
    buybacks = _f(last.get("buybacks")) or 0.0
    dividends = _f(last.get("dividends")) or 0.0
    shareholder_yield = (
        (buybacks + dividends) / market_cap * 100.0
        if (market_cap and (buybacks or dividends)) else None
    )
    # Compute from the canonical FCF & market cap so it agrees with P/FCF; the comps
    # target stores fcf_yield as a fraction, so fall back to that only if needed.
    if fcf and market_cap:
        fcf_yield = fcf / market_cap * 100.0
    else:
        tv = _f(target.get("fcf_yield"))
        fcf_yield = (tv * 100.0 if (tv is not None and abs(tv) < 1.5) else tv)
    p_fcf = (market_cap / fcf) if (market_cap and fcf) else None

    if not any([price, market_cap, target, last]):
        return {"available": False}

    # Trailing-twelve-months multiples (fresher than the fiscal-year figures) when
    # quarterly data is available for this filer. EV uses market_cap + net_debt.
    # Reuse analytics["quarterly"] computed in the engine node; fall back to a live
    # query (e.g. when rendering a report from a bare state).
    a_q = a.get("quarterly") or {}
    if a_q.get("available"):
        ttm = a_q.get("ttm")
    else:
        from . import quarterly as _q
        qser = _q.quarterly_series(state.get("cik"), state.get("jurisdiction") or "US")
        ttm = qser.get("ttm") if qser.get("available") else None
    ttm = ttm if (ttm and ttm.get("available")) else None
    ttm_block: dict[str, Any] = {"has_ttm": False}
    if ttm:
        t_ni = _f(ttm.get("net_income")); t_ebitda = _f(ttm.get("ebitda"))
        t_ebit = _f(ttm.get("ebit")); t_fcf = _f(ttm.get("free_cash_flow"))
        ev = enterprise_value
        ttm_block = {
            "has_ttm": True,
            "ttm_window": f'{ttm.get("start_period_end")}..{ttm.get("period_end")}',
            "ttm_period_end": ttm.get("period_end"),
            "ttm_revenue": _f(ttm.get("revenue")),
            "ttm_ebit": t_ebit,
            "ttm_ebitda": t_ebitda,
            "ttm_net_income": t_ni,
            "ttm_free_cash_flow": t_fcf,
            "ttm_ebit_margin_pct": _f(ttm.get("ebit_margin_pct")),
            "pe_ttm": (market_cap / t_ni) if (market_cap and t_ni) else None,
            "ev_ebitda_ttm": (ev / t_ebitda) if (ev and t_ebitda) else None,
            "ev_ebit_ttm": (ev / t_ebit) if (ev and t_ebit) else None,
            "ev_fcf_ttm": (ev / t_fcf) if (ev and t_fcf) else None,
            "fcf_yield_ttm_pct": (t_fcf / market_cap * 100.0) if (market_cap and t_fcf) else None,
            "p_fcf_ttm": (market_cap / t_fcf) if (market_cap and t_fcf) else None,
        }

    return {
        **ttm_block,
        "multiples_basis": "TTM" if ttm else "FY",
        "available": True,
        # --- market ---
        "as_of_price": price,
        "shares_out": shares,
        "market_cap": market_cap,
        "net_debt": net_debt,                       # negative = net cash
        "net_cash": (-net_debt) if net_debt else 0.0,
        "enterprise_value": enterprise_value,
        # --- valuation multiples (from the comps target row — matches the comps table) ---
        "pe": _f(target.get("pe")),
        "ev_ebitda": _f(target.get("ev_ebitda")),
        "ev_ebit": _f(target.get("ev_ebit")),
        "ev_revenue": _f(target.get("ev_revenue")),
        "ev_fcf": _f(target.get("ev_fcf")),
        "fcf_yield_pct": fcf_yield,
        "p_fcf": p_fcf,
        # --- returns / capital allocation ---
        "roic_pct": _f(last.get("roic_pct")) or (_f(target.get("roic")) * 100.0 if _f(target.get("roic")) else None),
        "incremental_roic_pct": _f(inc.get("incremental_roic_pct")),
        "incremental_roic_spread_pct": _f(inc.get("spread_vs_wacc_pct")),
        "shareholder_yield_pct": shareholder_yield,
        # --- latest-FY cash flow (one canonical record) ---
        "fiscal_year": last.get("fiscal_year"),
        "revenue": _f(last.get("revenue")),
        "operating_cash_flow": ocf,
        "capex": capex,
        "free_cash_flow": fcf,
        "capex_pct_revenue": _f(last.get("capex_pct_revenue")),
        # --- what the price implies (deterministic reverse-DCF) ---
        "reverse_dcf_implied_growth_pct": _f(rdcf.get("implied_growth_pct")),
        "reverse_dcf_implied_margin_pct": _f(rmargin.get("implied_margin_pct")),
        "reverse_dcf_margin_bounded": bool(rmargin.get("bounded")),
        "wacc_pct": _f(w.get("wacc_pct")),
        # --- market structure ---
        "ownership_quarter": own.get("quarter"),
    }
