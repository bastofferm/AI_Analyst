"""Deterministic scenario valuation.

Turns the Lead Analyst's three ``ScenarioAssumptions`` into a probability-weighted
fair value by running the existing ``services.corporate_dcf`` once per scenario.
No DCF math lives here — we only supply the assumption levers and weight the
resulting per-share values. ``corporate_dcf`` keeps its own deterministic share
count and net debt, so scenarios differ only in the levers we override.
"""
from __future__ import annotations

import math
import re
from typing import Any

from .. import services
from ..dcf_engine import DCF_HORIZON_YEARS
from . import wacc as wacc_mod

# Assumption levers we hand to corporate_dcf. share_count_mm is intentionally
# omitted so every scenario reuses the same deterministic diluted share count.
_LEVERS = (
    "rev_growth_pct",
    "terminal_growth_pct",
    "ebit_margin_pct",
    "tax_rate_pct",
    "capex_pct_of_rev",
    "nwc_pct_of_rev",
    "wacc_pct",
    "rationale",
)


def _scenario_to_assumptions(scenario: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _LEVERS:
        if scenario.get(k) is not None:
            out[k] = scenario[k]
    # A scenario with a single growth number tilt still needs a full-horizon vector.
    g = out.get("rev_growth_pct")
    if isinstance(g, (int, float)):
        out["rev_growth_pct"] = [float(g)] * DCF_HORIZON_YEARS
    return out


def run_scenarios(ticker: str, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Run upside/base/downside DCFs and weight them.

    Returns ``{"scenarios": [...], "probability_weighted_fair_value": float|None,
    "current_price": float|None, "implied_upside_pct": float|None, "implemented": bool}``.
    """
    results: list[dict[str, Any]] = []
    current_price: float | None = None
    implemented = False

    for sc in scenarios:
        label = sc.get("label", "base")
        weight = float(sc.get("weight", 0.0) or 0.0)
        dcf = services.corporate_dcf(ticker, assumptions=_scenario_to_assumptions(sc))
        implemented = implemented or bool(dcf.get("implemented"))
        per_share = dcf.get("per_share_value")
        if isinstance(per_share, float) and math.isnan(per_share):
            per_share = None
        if current_price is None and dcf.get("current_price") is not None:
            current_price = dcf.get("current_price")
        results.append({
            "label": label,
            "weight": weight,
            "per_share_value": per_share,
            "wacc_pct": sc.get("wacc_pct"),
            "terminal_growth_pct": sc.get("terminal_growth_pct"),
            "ebit_margin_pct": sc.get("ebit_margin_pct"),
            "rev_growth_pct": sc.get("rev_growth_pct"),
            "enterprise_value": dcf.get("enterprise_value"),
            "equity_value": dcf.get("equity_value"),
            "rationale": sc.get("rationale", ""),
            "implemented": bool(dcf.get("implemented")),
            "message": dcf.get("message"),
            # Retain the full per-year projection so the report can render the model.
            "dcf_full": {k: dcf.get(k) for k in (
                "projected_income", "projected_cashflow", "projected_balance_sheet",
                "fcfs", "discounted_fcfs", "terminal_value", "terminal_pv", "waterfall",
                "enterprise_value", "net_debt", "equity_value", "per_share_value",
                "current_price", "assumptions", "historicals_used")},
        })

    valued = [(r["weight"], r["per_share_value"]) for r in results if r["per_share_value"] is not None]
    total_w = sum(w for w, _ in valued)
    if valued and total_w > 0:
        pwfv = sum(w * ps for w, ps in valued) / total_w
    else:
        pwfv = None

    implied_upside = (
        (pwfv / current_price - 1.0) * 100.0
        if (pwfv is not None and current_price)
        else None
    )

    return {
        "scenarios": results,
        "probability_weighted_fair_value": pwfv,
        "current_price": current_price,
        "implied_upside_pct": implied_upside,
        "implemented": implemented,
    }


# --------------------------------------------------------- growth × WACC grid

def sensitivity_grid(ticker: str, base: dict[str, Any],
                     growth_deltas=(-3.0, -1.5, 0.0, 1.5, 3.0),
                     wacc_deltas=(-1.5, -0.75, 0.0, 0.75, 1.5)) -> dict[str, Any]:
    """Per-share value across a revenue-growth × WACC matrix around the base case."""
    g0 = base.get("rev_growth_pct")
    g0 = (sum(g0) / len(g0)) if isinstance(g0, list) and g0 else 5.0
    w0 = base.get("wacc_pct") or 9.0
    growth_axis = [round(g0 + d, 2) for d in growth_deltas]
    wacc_axis = [round(w0 + d, 2) for d in wacc_deltas]
    matrix: list[list[float | None]] = []
    for g in growth_axis:
        row: list[float | None] = []
        for w in wacc_axis:
            a = _scenario_to_assumptions({**base, "rev_growth_pct": [g] * DCF_HORIZON_YEARS, "wacc_pct": w})
            dcf = services.corporate_dcf(ticker, assumptions=a)
            ps = dcf.get("per_share_value")
            if isinstance(ps, float) and math.isnan(ps):
                ps = None
            row.append(round(ps, 2) if isinstance(ps, (int, float)) else None)
        matrix.append(row)
    return {"growth_axis": growth_axis, "wacc_axis": wacc_axis, "per_share": matrix,
            "base_growth": round(g0, 2), "base_wacc": round(w0, 2)}


# --------------------------------------------------- sum-of-the-parts (segments)

# Keyword heuristics: growth/AI segments carry a higher discount rate + growth,
# mature cash-cow segments a lower one. Overridable by LLM-supplied seg_params.
_HIGH_BETA = re.compile(r"(?i)cloud|azure|ai|data|intelligent|advertis|search|platform|services")
_LOW_BETA = re.compile(r"(?i)personal comput|windows|device|hardware|legacy|mature|gaming")


def default_segment_params(segments: list[dict[str, Any]], base_growth: float) -> dict[str, dict[str, float]]:
    params: dict[str, dict[str, float]] = {}
    for s in segments:
        name = s.get("segment", "")
        margin = s.get("operating_margin")
        # conversion = share of NOPAT that becomes FCF after capex/D&A/NWC. Growth/
        # AI segments carry heavy capex (low conversion); mature segments convert well.
        if _HIGH_BETA.search(name):
            g, bdelta, prem, conv = base_growth + 4.0, 0.15, 75.0, 0.55
        elif _LOW_BETA.search(name):
            g, bdelta, prem, conv = max(0.0, base_growth - 3.0), -0.10, 0.0, 0.85
        else:
            g, bdelta, prem, conv = base_growth, 0.0, 25.0, 0.88
        params[name] = {
            "growth_pct": round(g, 1),
            "operating_margin_pct": round((margin * 100.0) if margin is not None else 25.0, 1),
            "beta_delta": bdelta,
            "growth_premium_bp": prem,
            "fcf_conversion": conv,
        }
    return params


def sotp_valuation(segments: list[dict[str, Any]], wacc_base: dict[str, Any], *,
                   net_debt: float, shares: float, base_growth: float,
                   seg_params: dict[str, dict[str, float]] | None = None,
                   terminal_growth: float = 2.5, fcf_conversion: float = 0.85,
                   years: int = DCF_HORIZON_YEARS) -> dict[str, Any]:
    """Value each reportable segment on its own growth/margin/WACC, then aggregate.

    Simplified segment DCF (segment capex/tax not separately disclosed): segment
    FCF ~= NOPAT × conversion, discounted at a segment-specific WACC derived from
    the company FF-WACC with a beta shift. Segment EVs sum to enterprise value.
    """
    if not segments:
        return {"available": False}
    seg_params = seg_params or default_segment_params(segments, base_growth)
    tax = (wacc_base.get("tax_rate_pct", 21.0)) / 100.0
    rows: list[dict[str, Any]] = []
    total_ev = 0.0
    for s in segments:
        name = s.get("segment", "")
        rev0 = s.get("revenue")
        if not rev0:
            continue
        p = seg_params.get(name) or default_segment_params([s], base_growth)[name]
        g = p["growth_pct"] / 100.0
        margin = p["operating_margin_pct"] / 100.0
        conv = p.get("fcf_conversion", fcf_conversion)
        seg_wacc = wacc_mod.segment_wacc(wacc_base, p.get("beta_delta", 0.0), p.get("growth_premium_bp", 0.0)) / 100.0
        gt = terminal_growth / 100.0
        rev = rev0
        pv = 0.0
        last_fcf = 0.0
        for yr in range(1, years + 1):
            rev *= (1 + g)
            nopat = rev * margin * (1 - tax)
            fcf = nopat * conv
            pv += fcf / ((1 + seg_wacc) ** yr)
            last_fcf = fcf
        tv = (last_fcf * (1 + gt) / (seg_wacc - gt)) if seg_wacc > gt else 0.0
        ev = pv + tv / ((1 + seg_wacc) ** years)
        total_ev += ev
        rows.append({
            "segment": name, "revenue": rev0, "growth_pct": p["growth_pct"],
            "operating_margin_pct": p["operating_margin_pct"], "wacc_pct": round(seg_wacc * 100, 2),
            "fcf_conversion": round(conv, 2), "enterprise_value": ev,
        })
    equity = total_ev - (net_debt or 0.0)
    per_share = (equity / shares) if shares else None
    return {
        "available": True,
        "segments": rows,
        "enterprise_value": total_ev,
        "net_debt": net_debt or 0.0,
        "equity_value": equity,
        "per_share_value": per_share,
        "shares": shares,
        "terminal_growth_pct": terminal_growth,
    }


def sotp_scenarios(segments: list[dict[str, Any]], wacc_base: dict[str, Any], *,
                   net_debt: float, shares: float, base_growth: float,
                   weights: dict[str, float] | None = None,
                   seg_params: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """SOTP under upside / base / downside segment assumptions → probability-weighted per share.

    Upside lifts each segment's growth (+3pp) and margin (+2pp); downside cuts them
    (−3pp / −2pp). This is the *primary* valuation method.
    """
    if not segments:
        return {"available": False}
    weights = weights or {"upside": 0.2, "base": 0.6, "downside": 0.2}
    base_params = seg_params or default_segment_params(segments, base_growth)

    def tilt(dg: float, dm: float) -> dict[str, dict[str, float]]:
        out = {}
        for name, p in base_params.items():
            out[name] = {**p,
                         "growth_pct": max(0.0, p["growth_pct"] + dg),
                         "operating_margin_pct": max(0.0, p["operating_margin_pct"] + dm)}
        return out

    cases = {
        "upside": sotp_valuation(segments, wacc_base, net_debt=net_debt, shares=shares,
                                 base_growth=base_growth, seg_params=tilt(3.0, 2.0), terminal_growth=3.0),
        "base": sotp_valuation(segments, wacc_base, net_debt=net_debt, shares=shares,
                               base_growth=base_growth, seg_params=base_params, terminal_growth=2.5),
        "downside": sotp_valuation(segments, wacc_base, net_debt=net_debt, shares=shares,
                                   base_growth=base_growth, seg_params=tilt(-1.5, -1.0), terminal_growth=2.25),
    }
    ps = {k: v.get("per_share_value") for k, v in cases.items()}
    valued = [(weights.get(k, 0), ps[k]) for k in cases if isinstance(ps[k], (int, float))]
    tw = sum(w for w, _ in valued)
    weighted = (sum(w * p for w, p in valued) / tw) if tw else None
    return {
        "available": True,
        "cases": cases,
        "per_share": ps,
        "weights": weights,
        "weighted_per_share": weighted,
        "primary_per_share": ps.get("base"),
        "segments_base": cases["base"].get("segments"),
    }


# ----------------------------------------------------------- reverse DCF

def reverse_dcf(ticker: str, base: dict[str, Any], current_price: float,
                lo: float = -5.0, hi: float = 45.0, iters: int = 26) -> dict[str, Any]:
    """Solve for the flat revenue growth the current price implies (bisection).

    Everything except revenue growth is held at the base scenario. Answers
    "what is the market pricing in?" — the calibration check for the thesis.
    """
    if not current_price or current_price <= 0:
        return {"available": False}

    def ps_at(g: float) -> float | None:
        a = _scenario_to_assumptions({**base, "rev_growth_pct": [g] * DCF_HORIZON_YEARS})
        dcf = services.corporate_dcf(ticker, assumptions=a)
        v = dcf.get("per_share_value")
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else v

    p_lo, p_hi = ps_at(lo), ps_at(hi)
    if p_lo is None or p_hi is None:
        return {"available": False}
    if not (min(p_lo, p_hi) <= current_price <= max(p_lo, p_hi)):
        # Price outside the achievable band — report the nearer bound.
        implied = hi if abs(p_hi - current_price) < abs(p_lo - current_price) else lo
        return {"available": True, "implied_growth_pct": round(implied, 1), "bounded": True,
                "note": "market price implies growth beyond the tested range"}
    a, b = lo, hi
    for _ in range(iters):
        m = (a + b) / 2
        pm = ps_at(m)
        if pm is None:
            break
        if (pm > current_price) == (p_hi > current_price):
            b = m
        else:
            a = m
    return {"available": True, "implied_growth_pct": round((a + b) / 2, 1), "bounded": False,
            "base_growth_pct": round(sum(base.get("rev_growth_pct", [5]))/max(1,len(base.get("rev_growth_pct",[5]))), 1)}


def reverse_dcf_margin(ticker: str, base: dict[str, Any], current_price: float,
                       peer_max_margin_pct: float | None = None,
                       lo: float = 5.0, hi: float = 70.0, iters: int = 26) -> dict[str, Any]:
    """Solve for the steady EBIT margin the current price implies (bisection).

    Revenue growth is *frozen* at the base path, so capex / D&A / ΔNWC (all
    revenue-driven) are fixed and the only free lever in the FCFF numerator is the
    operating margin m. Answers: "how profitable must the company become to justify
    today's price?" — the sharpest read on the operating leverage / pricing power
    the market is underwriting. Reports the implied margin, the shift vs today's
    margin (bp), and whether it clears best-in-class peer profitability.
    """
    if not current_price or current_price <= 0:
        return {"available": False}
    base_margin = float(base.get("ebit_margin_pct") or 0.0)

    def ps_at(m: float) -> float | None:
        a = _scenario_to_assumptions({**base, "ebit_margin_pct": m})
        dcf = services.corporate_dcf(ticker, assumptions=a)
        v = dcf.get("per_share_value")
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else v

    p_lo, p_hi = ps_at(lo), ps_at(hi)
    if p_lo is None or p_hi is None:
        return {"available": False}
    if not (min(p_lo, p_hi) <= current_price <= max(p_lo, p_hi)):
        # Even the extreme margin cannot reach the price at this frozen growth.
        implied = hi if abs(p_hi - current_price) < abs(p_lo - current_price) else lo
        return {"available": True, "implied_margin_pct": round(implied, 1), "bounded": True,
                "base_margin_pct": round(base_margin, 1),
                "implied_shift_bp": round((implied - base_margin) * 100, 0),
                "exceeds_peer_max": (peer_max_margin_pct is not None and implied > peer_max_margin_pct),
                "peer_max_margin_pct": peer_max_margin_pct,
                "note": "price unreachable within a plausible margin band at frozen growth — priced for perfection"}
    a, b = lo, hi
    for _ in range(iters):
        mid = (a + b) / 2
        pm = ps_at(mid)
        if pm is None:
            break
        if (pm > current_price) == (p_hi > current_price):
            b = mid
        else:
            a = mid
    implied = (a + b) / 2
    return {"available": True, "implied_margin_pct": round(implied, 1), "bounded": False,
            "base_margin_pct": round(base_margin, 1),
            "implied_shift_bp": round((implied - base_margin) * 100, 0),
            "exceeds_peer_max": (peer_max_margin_pct is not None and implied > peer_max_margin_pct),
            "peer_max_margin_pct": round(peer_max_margin_pct, 1) if peer_max_margin_pct else None,
            "frozen_growth_pct": round(sum(base.get("rev_growth_pct", [5]))/max(1, len(base.get("rev_growth_pct", [5]))), 1)}


# ----------------------------------------------------------- triangulation

def triangulate(sotp: dict[str, Any], dcf_scen: dict[str, Any], comps_implied: dict[str, Any] | None,
                current_price: float | None, shares: float | None) -> dict[str, Any]:
    """Assemble the football-field ranges and the SOTP-primary headline value.

    Per user decision: SOTP is the primary fair value; consolidated DCF and
    peer-multiples-implied values are corroborating cross-checks.
    """
    methods: list[dict[str, Any]] = []

    if sotp and sotp.get("available"):
        ps = sotp.get("per_share") or {}
        lows = [v for v in (ps.get("downside"), ps.get("base"), ps.get("upside")) if isinstance(v, (int, float))]
        if lows:
            methods.append({"label": "Sum-of-the-parts", "low": min(lows), "high": max(lows),
                            "mid": ps.get("base"), "primary": True})

    if dcf_scen and dcf_scen.get("scenarios"):
        vals = [s.get("per_share_value") for s in dcf_scen["scenarios"] if isinstance(s.get("per_share_value"), (int, float))]
        if vals:
            methods.append({"label": "Consolidated DCF", "low": min(vals), "high": max(vals),
                            "mid": dcf_scen.get("probability_weighted_fair_value")})

    if comps_implied and shares:
        eq = [v for k, v in comps_implied.items() if k.endswith("_equity") and isinstance(v, (int, float)) and v > 0]
        if eq and shares:
            ps_vals = sorted(v / shares for v in eq)
            methods.append({"label": "Peer multiples", "low": ps_vals[0], "high": ps_vals[-1],
                            "mid": ps_vals[len(ps_vals) // 2]})

    primary = next((m["mid"] for m in methods if m.get("primary")), None)
    if primary is None and methods:
        primary = methods[0].get("mid")
    upside = ((primary / current_price - 1) * 100.0) if (primary and current_price) else None
    all_mids = [m["mid"] for m in methods if isinstance(m.get("mid"), (int, float))]
    return {
        "methods": methods,
        "primary_fair_value": primary,
        "primary_method": "Sum-of-the-parts",
        "implied_upside_pct": upside,
        "blended_fair_value": (sum(all_mids) / len(all_mids)) if all_mids else None,
        "current_price": current_price,
    }
