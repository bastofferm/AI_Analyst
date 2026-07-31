"""Pure-Python DCF math.

Projection logic is intentionally simple and transparent so the equity research
"why" of every number is easy to read off the projected statements:

  Revenue   grows by rev_growth_pct[i] per year (7 years).
  EBIT      = Revenue * ebit_margin_pct.
  Tax       = max(EBIT, 0) * tax_rate_pct.
  NOPAT     = EBIT - Tax.
  D&A       = D&A% of revenue (taken from latest historical ratio if available).
  Capex     = capex_pct_of_rev * Revenue.
  ΔNWC      = nwc_pct_of_rev * ΔRevenue.
  FCF       = NOPAT + D&A - Capex - ΔNWC.
  TV        = FCF_year7 * (1 + g) / (WACC - g).
  EV        = sum(FCF_i / (1+WACC)^i) + TV / (1+WACC)^7.
  Equity    = EV - net_debt (latest historical).
  Per share = Equity / share_count.

Sensitivity = 5×5 grid varying WACC ± 200 bp and terminal growth ± 100 bp.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Explicit-forecast horizon (years) for the consolidated DCF. The terminal-value
# discount exponent below keys off len(fcfs), so changing this stays consistent.
DCF_HORIZON_YEARS = 7


@dataclass
class Historicals:
    revenue: float          # latest fiscal year
    ebit: float | None
    da: float | None        # depreciation & amortisation
    capex: float | None     # positive number, magnitude
    nwc_chg: float | None
    net_debt: float | None
    da_pct_of_rev: float    # ratio used for projection
    nwc_pct_of_rev: float
    capex_pct_of_rev: float
    fiscal_year: int | None


def normalise_assumptions(a: dict) -> dict:
    """Coerce LLM-supplied JSON to a fully populated assumption dict (percent inputs)."""
    g = list(a.get("rev_growth_pct") or [])
    while len(g) < DCF_HORIZON_YEARS: g.append(g[-1] if g else 3.0)
    return {
        "rev_growth_pct":     [float(x) for x in g[:DCF_HORIZON_YEARS]],
        "terminal_growth_pct": float(a.get("terminal_growth_pct", 2.5)),
        "ebit_margin_pct":    float(a.get("ebit_margin_pct", 15.0)),
        "tax_rate_pct":       float(a.get("tax_rate_pct", 21.0)),
        "capex_pct_of_rev":   float(a.get("capex_pct_of_rev", 4.0)),
        "nwc_pct_of_rev":     float(a.get("nwc_pct_of_rev", 2.0)),
        "wacc_pct":           float(a.get("wacc_pct", 9.0)),
        "share_count_mm":     float(a.get("share_count_mm", 0.0) or 0.0),
        "rationale":          str(a.get("rationale", "") or ""),
    }


def run(assumptions: dict, hist: Historicals,
        current_price: float | None = None) -> dict:
    a = normalise_assumptions(assumptions)
    rev = []
    last = hist.revenue
    for g_pct in a["rev_growth_pct"]:
        last = last * (1.0 + g_pct / 100.0)
        rev.append(last)
    ebit_margin = a["ebit_margin_pct"] / 100.0
    tax_rate    = max(0.0, a["tax_rate_pct"] / 100.0)
    capex_pct   = a["capex_pct_of_rev"] / 100.0
    nwc_pct     = a["nwc_pct_of_rev"] / 100.0
    da_pct      = max(0.0, hist.da_pct_of_rev)
    wacc        = a["wacc_pct"] / 100.0
    g_term      = a["terminal_growth_pct"] / 100.0

    proj_income: list[dict] = []
    proj_cf: list[dict] = []
    fcfs: list[float] = []
    prev_rev = hist.revenue

    for i, r in enumerate(rev, start=1):
        ebit = r * ebit_margin
        tax  = max(ebit, 0.0) * tax_rate
        nopat = ebit - tax
        da   = r * da_pct
        capex = r * capex_pct
        d_nwc = (r - prev_rev) * nwc_pct
        fcf  = nopat + da - capex - d_nwc
        fcfs.append(fcf)
        proj_income.append({"year": f"Y{i}", "revenue": r, "ebit": ebit,
                              "tax": -tax, "nopat": nopat})
        proj_cf.append({"year": f"Y{i}", "nopat": nopat, "d_a": da,
                          "capex": -capex, "d_nwc": -d_nwc, "fcf": fcf})
        prev_rev = r

    discounted = [fcf / ((1.0 + wacc) ** (i + 1)) for i, fcf in enumerate(fcfs)]
    if wacc <= g_term:
        terminal_value = float("nan")
        terminal_pv    = float("nan")
    else:
        terminal_value = fcfs[-1] * (1.0 + g_term) / (wacc - g_term)
        terminal_pv    = terminal_value / ((1.0 + wacc) ** len(fcfs))
    ev = sum(discounted) + (terminal_pv if terminal_pv == terminal_pv else 0.0)
    equity = ev - (hist.net_debt or 0.0)
    shares = a["share_count_mm"] * 1_000_000.0
    per_share = (equity / shares) if shares > 0 else float("nan")
    upside = ((per_share / current_price) - 1.0) * 100.0 if (current_price and per_share == per_share) else None

    proj_bs: list[dict] = []
    prev_assets = hist.revenue
    for i, r in enumerate(rev, start=1):
        wc_addition = (r - (rev[i-2] if i >= 2 else hist.revenue)) * nwc_pct
        prev_assets = prev_assets + wc_addition
        proj_bs.append({"year": f"Y{i}", "implied_invested_capital": prev_assets,
                          "working_capital_addition": wc_addition})

    sens = _sensitivity_grid(fcfs, hist.net_debt or 0.0, shares, wacc, g_term)

    waterfall = [
        {"name": "PV of explicit FCFs", "value": sum(discounted)},
        {"name": "PV of terminal value", "value": terminal_pv if terminal_pv == terminal_pv else 0.0},
        {"name": "Enterprise value", "value": ev, "is_total": True},
        {"name": "Less: net debt", "value": -(hist.net_debt or 0.0)},
        {"name": "Equity value", "value": equity, "is_total": True},
    ]

    return {
        "assumptions": a,
        "projected_income": proj_income,
        "projected_balance_sheet": proj_bs,
        "projected_cashflow": proj_cf,
        "fcfs": fcfs,
        "discounted_fcfs": discounted,
        "terminal_value": terminal_value,
        "terminal_pv": terminal_pv,
        "enterprise_value": ev,
        "net_debt": hist.net_debt or 0.0,
        "equity_value": equity,
        "shares": shares,
        "per_share_value": per_share,
        "current_price": current_price,
        "upside_pct": upside,
        "sensitivity": sens,
        "waterfall": waterfall,
        "historicals_used": {
            "revenue":          hist.revenue,
            "ebit":             hist.ebit,
            "d_a":              hist.da,
            "capex":            hist.capex,
            "net_debt":         hist.net_debt,
            "da_pct_of_rev":    hist.da_pct_of_rev,
            "fiscal_year":      hist.fiscal_year,
        },
    }


def _sensitivity_grid(fcfs: list[float], net_debt: float, shares: float,
                      wacc: float, g_term: float) -> dict:
    wacc_pts = [wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02]
    g_pts    = [g_term - 0.01, g_term - 0.005, g_term, g_term + 0.005, g_term + 0.01]
    z: list[list[float]] = []
    for w in wacc_pts:
        row = []
        for g in g_pts:
            if w <= g:
                row.append(float("nan"))
                continue
            disc  = sum(fcf / ((1.0 + w) ** (i + 1)) for i, fcf in enumerate(fcfs))
            tv    = fcfs[-1] * (1.0 + g) / (w - g)
            tv_pv = tv / ((1.0 + w) ** len(fcfs))
            ev    = disc + tv_pv
            equity = ev - net_debt
            ps = (equity / shares) if shares > 0 else float("nan")
            row.append(ps)
        z.append(row)
    return {
        "wacc_axis":   [round(w * 100, 2) for w in wacc_pts],
        "g_axis":      [round(g * 100, 2) for g in g_pts],
        "per_share":   z,
    }


def build_historicals_from_fundamentals(fund_table: dict) -> Historicals | None:
    """Derive a Historicals dataclass from the output of tools.get_fundamentals."""
    cols = fund_table.get("columns") or []
    rows = fund_table.get("rows") or []
    if not cols or not rows:
        return None
    fy_idx = cols.index("fiscal_year") if "fiscal_year" in cols else 0
    def col(name): return cols.index(name) if name in cols else None
    rev_i, ebit_i, fcf_i = col("revenue"), col("earnings_before_interest_taxes"), col("free_cash_flow")
    capex_i, cfo_i = col("capital_expenditures"), col("cash_flow_from_operations")
    nd_i = col("net_debt")

    rows_sorted = sorted(rows, key=lambda r: (r[fy_idx] is None, r[fy_idx]), reverse=True)
    if not rows_sorted:
        return None
    top = rows_sorted[0]

    def g(i):
        if i is None: return None
        v = top[i]
        try: return float(v) if v is not None else None
        except (TypeError, ValueError): return None

    revenue = g(rev_i)
    if not revenue or revenue <= 0:
        return None
    ebit = g(ebit_i)
    capex = abs(g(capex_i)) if g(capex_i) is not None else None
    cfo = g(cfo_i)
    fcf = g(fcf_i)
    da = (cfo - ebit) if (cfo is not None and ebit is not None) else None
    net_debt = g(nd_i)

    da_pct = (da / revenue) if (da is not None and revenue) else 0.04
    capex_pct = (capex / revenue) if (capex is not None and revenue) else 0.04
    nwc_pct = 0.02

    return Historicals(
        revenue=revenue,
        ebit=ebit,
        da=da,
        capex=capex,
        nwc_chg=None,
        net_debt=net_debt,
        da_pct_of_rev=max(0.0, da_pct),
        nwc_pct_of_rev=nwc_pct,
        capex_pct_of_rev=max(0.0, capex_pct),
        fiscal_year=int(top[fy_idx]) if top[fy_idx] is not None else None,
    )
