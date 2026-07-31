"""Full projected model built ON TOP of the DCF engine's own projection.

`dcf_engine.run()` already projects revenue → EBIT → NOPAT → FCFF per year and the
EV→equity waterfall behind the reported per-share. This module takes that exact
output and *enriches* it into a full projected income statement (COGS, gross
profit, operating expenses, interest, pre-tax, tax, net income, EPS) plus the FCFF
build and the valuation bridge — so the rendered model FOOTS to the headline number
(same revenue, EBIT, FCFF, EV as the engine) while showing the full P&L.

SG&A/R&D are not split in the standardized data, so operating expenses appear as a
single line (= gross profit − EBIT). Interest is held at the current level; the
projection holds share count ~flat (buyback effect noted qualitatively in the memo).
"""
from __future__ import annotations

from typing import Any


def build_income_statement_model(
    dcf_full: dict[str, Any], *,
    gross_margin_pct: float,
    interest_expense: float | None,
    debt: float | None,
    cost_of_debt_pct: float | None,
    tax_rate_pct: float,
    shares: float | None,
    base_revenue: float | None,
) -> dict[str, Any]:
    proj_income = dcf_full.get("projected_income") or []
    proj_cf = dcf_full.get("projected_cashflow") or []
    discounted = dcf_full.get("discounted_fcfs") or []
    fcfs = dcf_full.get("fcfs") or []
    a = dcf_full.get("assumptions") or {}
    wacc = (a.get("wacc_pct") or 9.0) / 100.0
    gm = (gross_margin_pct or 60.0) / 100.0
    t = max(0.0, (tax_rate_pct or 21.0)) / 100.0
    interest = abs(interest_expense) if interest_expense else (
        (debt or 0.0) * (cost_of_debt_pct or 0.0) / 100.0)

    years: list[dict[str, Any]] = []
    prev_rev = base_revenue
    for i, inc in enumerate(proj_income):
        rev = inc.get("revenue")
        ebit = inc.get("ebit")
        if rev is None or ebit is None:
            continue
        growth = ((rev / prev_rev - 1.0) * 100.0) if prev_rev else None
        cogs = rev * (1.0 - gm)
        gross = rev * gm
        opex = gross - ebit                      # SG&A + R&D + other opex (combined)
        pretax = ebit - interest
        tax_amt = max(0.0, pretax) * t
        net_income = pretax - tax_amt
        eps = (net_income / shares) if shares else None
        cf = proj_cf[i] if i < len(proj_cf) else {}
        disc_factor = 1.0 / ((1.0 + wacc) ** (i + 1))
        years.append({
            "year": inc.get("year", f"Y{i+1}"),
            "revenue": rev, "growth_pct": growth,
            "cogs": cogs, "gross_profit": gross, "gross_margin_pct": gm * 100.0,
            "operating_expenses": opex, "ebit": ebit,
            "ebit_margin_pct": (ebit / rev * 100.0) if rev else None,
            "interest": interest, "pretax": pretax, "tax": tax_amt,
            "net_income": net_income, "eps": eps,
            # FCFF build (straight from the engine, unlevered)
            "nopat": cf.get("nopat"), "d_a": cf.get("d_a"),
            "capex": cf.get("capex"), "d_nwc": cf.get("d_nwc"),
            "fcff": cf.get("fcf") if cf else (fcfs[i] if i < len(fcfs) else None),
            "discount_factor": disc_factor,
            "pv_fcff": discounted[i] if i < len(discounted) else None,
        })
        prev_rev = rev

    return {
        "years": years,
        "waterfall": dcf_full.get("waterfall"),
        "terminal_value": dcf_full.get("terminal_value"),
        "terminal_pv": dcf_full.get("terminal_pv"),
        "sum_pv_fcff": sum(x for x in discounted if isinstance(x, (int, float))),
        "enterprise_value": dcf_full.get("enterprise_value"),
        "net_debt": dcf_full.get("net_debt"),
        "equity_value": dcf_full.get("equity_value"),
        "per_share_value": dcf_full.get("per_share_value"),
        "current_price": dcf_full.get("current_price"),
        "shares": shares,
        "assumptions": a,
        "gross_margin_pct": gm * 100.0,
        "interest_expense": interest,
    }


def compact_summary(model: dict[str, Any]) -> dict[str, Any]:
    """A small dict the reasoner can cite (final-year IS + bridge)."""
    yrs = model.get("years") or []
    if not yrs:
        return {}
    last = yrs[-1]
    return {
        "final_year_revenue": last.get("revenue"),
        "final_year_ebit": last.get("ebit"),
        "final_year_net_income": last.get("net_income"),
        "final_year_eps": last.get("eps"),
        "final_year_fcff": last.get("fcff"),
        "sum_pv_fcff": model.get("sum_pv_fcff"),
        "terminal_pv": model.get("terminal_pv"),
        "enterprise_value": model.get("enterprise_value"),
        "equity_value": model.get("equity_value"),
        "per_share_value": model.get("per_share_value"),
        "assumptions": model.get("assumptions"),
    }
