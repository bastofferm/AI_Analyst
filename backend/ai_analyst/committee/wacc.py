"""Cost of capital from the Fama-French factor model.

Cost of equity is built bottom-up from the company's estimated factor betas
(``fact_factor_loadings``), long-run factor premia (``fact_fama_french``) and the
current risk-free rate (10Y Treasury, ``fact_macro`` FRED:DGS10):

    Re(FF5) = Rf + b_mkt·ERP + b_smb·SMB + b_hml·HML + b_rmw·RMW + b_cma·CMA
    Re(CAPM) = Rf + b_mkt·ERP

WACC = E/V·Re + D/V·Rd·(1-t), with E = market cap, D = total financial debt,
Rd = interest expense / debt (floored to Rf + spread), t = effective tax rate.

All values are percent. Everything degrades to sensible defaults when a company
has no factor regression yet, so downstream valuation never crashes.
"""
from __future__ import annotations

from typing import Any

from .._db import read_sql
from .. import services

_FF5_DATASET = "Developed_5_Factors_Daily"
_MOM_DATASET = "Developed_Mom_Factor_Daily"
_TRADING_DAYS = 252

# Sane fallbacks (percent) when data is missing.
_DEFAULT_RF = 4.3
_DEFAULT_ERP = 5.5
_DEFAULT_WACC = 9.0
_MIN_WACC, _MAX_WACC = 6.0, 16.0


def risk_free_rate() -> float:
    df = read_sql(
        "SELECT value FROM fact_macro WHERE series_id = 'FRED:DGS10' AND value IS NOT NULL "
        "ORDER BY date DESC LIMIT 1"
    )
    if df.empty:
        return _DEFAULT_RF
    try:
        return float(df.iloc[0]["value"])
    except (TypeError, ValueError):
        return _DEFAULT_RF


def factor_premia() -> dict[str, float]:
    """Annualized long-run factor premia (percent). Keys: mkt, smb, hml, rmw, cma, mom."""
    premia: dict[str, float] = {}
    df = read_sql(
        """
        SELECT factor, AVG(return_pct) AS avg_daily
        FROM fact_fama_french
        WHERE dataset = %(ds)s AND return_pct IS NOT NULL
          AND factor IN ('Mkt-RF','SMB','HML','RMW','CMA')
        GROUP BY factor
        """,
        {"ds": _FF5_DATASET},
    )
    name = {"Mkt-RF": "mkt", "SMB": "smb", "HML": "hml", "RMW": "rmw", "CMA": "cma"}
    for r in df.to_dict("records"):
        try:
            premia[name[r["factor"]]] = float(r["avg_daily"]) * _TRADING_DAYS
        except (TypeError, ValueError, KeyError):
            continue
    mom = read_sql(
        "SELECT AVG(return_pct) AS a FROM fact_fama_french WHERE dataset=%(ds)s AND return_pct IS NOT NULL",
        {"ds": _MOM_DATASET},
    )
    if not mom.empty and mom.iloc[0]["a"] is not None:
        premia["mom"] = float(mom.iloc[0]["a"]) * _TRADING_DAYS
    premia.setdefault("mkt", _DEFAULT_ERP)
    return premia


def _betas(ticker: str) -> dict[str, Any]:
    fx = services.factor_exposure(ticker)
    rows = fx.get("rows") or []
    if not rows:
        return {}
    row = rows[0]  # services orders FF6 first, then most recent window
    return {
        "model": row.get("model"),
        "window_end": row.get("window_end"),
        "adj_r2": row.get("adj_r2"),
        "mkt": row.get("beta_mkt"), "smb": row.get("beta_smb"), "hml": row.get("beta_hml"),
        "rmw": row.get("beta_rmw"), "cma": row.get("beta_cma"), "mom": row.get("beta_mom"),
    }


def _num(v: Any, default: float | None = None) -> float | None:
    try:
        f = float(v)
        return f if f == f else default  # drop NaN
    except (TypeError, ValueError):
        return default


def compute_wacc(ticker: str, packet: dict[str, Any] | None = None) -> dict[str, Any]:
    packet = packet or {}
    rf = risk_free_rate()
    premia = factor_premia()
    betas = _betas(ticker)

    beta_mkt = _num(betas.get("mkt"), 1.0) or 1.0
    re_capm = rf + beta_mkt * premia.get("mkt", _DEFAULT_ERP)

    # FF5 (+ momentum if available) multi-factor cost of equity.
    contrib = {"mkt": beta_mkt * premia.get("mkt", _DEFAULT_ERP)}
    for f in ("smb", "hml", "rmw", "cma", "mom"):
        b = _num(betas.get(f))
        if b is not None and f in premia:
            contrib[f] = b * premia[f]
    re_ff = rf + sum(contrib.values())
    # Guard against pathological multi-factor blow-ups; keep Re in a defensible band.
    re_ff = min(max(re_ff, rf + 2.0), rf + 12.0)
    re_capm = min(max(re_capm, rf + 2.0), rf + 12.0)

    # Capital structure + cost of debt. Pull the income/debt inputs directly from
    # the standardized facts (more reliable than the display-profile snapshot).
    facts = {**_fact_map(packet), **_wacc_inputs(ticker)}
    market_cap = _latest_market_cap(packet, ticker)
    debt = _num(facts.get("total_financial_debt")) or 0.0
    interest = abs(_num(facts.get("interest_expense")) or 0.0)
    ebit = _num(facts.get("earnings_before_interest_taxes")) or _num(facts.get("operating_income"))
    pretax = _num(facts.get("earnings_before_taxes")) or _num(facts.get("pretax_income"))
    tax_exp = _num(facts.get("income_tax_provision")) or _num(facts.get("income_tax_expense"))
    tax_rate = 21.0
    if pretax and tax_exp is not None and pretax > 0:
        tax_rate = max(0.0, min(35.0, abs(tax_exp) / pretax * 100.0))  # provision may be signed negative

    # Cost of debt = risk-free + a synthetic credit spread from interest coverage
    # (Damodaran-style large-cap rating table). The realized interest/debt rate is
    # reported as a cross-check but only blended in when it looks plausible (the
    # reported interest line often includes leases/other, inflating the ratio).
    coverage = (ebit / interest) if (ebit and interest) else None
    spread = _credit_spread(coverage)
    rd = rf + spread
    effective_rd = (interest / debt * 100.0) if (debt and interest) else None
    if effective_rd is not None and rf - 1.0 <= effective_rd <= rf + 3.0:
        rd = (rd + effective_rd) / 2.0
    rd = min(max(rd, rf), rf + 9.0)

    e = market_cap or 0.0
    v = e + debt
    we = (e / v) if v else 1.0
    wd = (debt / v) if v else 0.0
    # Headline WACC uses CAPM with the FF-estimated market beta — the market
    # convention and a stable discount rate. The full FF5 multi-factor Re is
    # reported alongside as an analytical exhibit (it can be unstable for growth
    # names whose value/momentum loadings drag the implied required return).
    re = re_capm
    wacc = we * re + wd * rd * (1 - tax_rate / 100.0)
    wacc = min(max(wacc, _MIN_WACC), _MAX_WACC)

    return {
        "wacc_pct": round(wacc, 2),
        "cost_of_equity_pct": round(re_capm, 2),
        "cost_of_equity_capm_pct": round(re_capm, 2),
        "cost_of_equity_ff5_pct": round(re_ff, 2),
        "cost_of_debt_pct": round(rd, 2),
        "credit_spread_pct": round(spread, 2),
        "interest_coverage": round(coverage, 1) if coverage else None,
        "effective_interest_rate_pct": round(effective_rd, 2) if effective_rd else None,
        "risk_free_pct": round(rf, 2),
        "equity_risk_premium_pct": round(premia.get("mkt", _DEFAULT_ERP), 2),
        "tax_rate_pct": round(tax_rate, 1),
        "weight_equity": round(we, 3),
        "weight_debt": round(wd, 3),
        "market_cap": market_cap,
        "total_debt": debt,
        "ebit": ebit,
        "interest_expense": interest,
        "betas": {k: _num(betas.get(k)) for k in ("mkt", "smb", "hml", "rmw", "cma", "mom")},
        "factor_model": betas.get("model"),
        "adj_r2": _num(betas.get("adj_r2")),
        "premia_pct": {k: round(v, 2) for k, v in premia.items()},
        "factor_contrib_pct": {k: round(v, 2) for k, v in contrib.items()},
        "audit_trail": [
            f"Rf (10Y UST) = {rf:.2f}%",
            f"ERP (FF Mkt-RF, annualized) = {premia.get('mkt', _DEFAULT_ERP):.2f}%",
            f"beta (FF {betas.get('model')}) = {beta_mkt:.2f}",
            f"Re (CAPM) = Rf + beta*ERP = {re_capm:.2f}%",
            f"Interest coverage = EBIT/Interest = {coverage:.1f}x" if coverage else "Interest coverage = n/a",
            f"Credit spread (synthetic rating) = {spread:.2f}%",
            f"Rd = Rf + spread (blended w/ realized) = {rd:.2f}%",
            f"Weights: E={we*100:.1f}% D={wd*100:.1f}%; tax = {tax_rate:.1f}%",
            f"WACC = We*Re + Wd*Rd*(1-t) = {wacc:.2f}%",
        ],
    }


# Damodaran-style synthetic-rating spread from interest coverage (large-cap, %).
_COVERAGE_SPREAD = [
    (8.5, 0.60), (6.5, 0.75), (5.5, 0.95), (4.25, 1.15), (3.0, 1.40),
    (2.5, 1.85), (2.0, 2.40), (1.5, 3.50), (1.25, 4.75), (0.8, 6.50), (0.5, 8.50),
]


def _credit_spread(coverage: float | None) -> float:
    if coverage is None:
        return 1.25
    for threshold, spread in _COVERAGE_SPREAD:
        if coverage >= threshold:
            return spread
    return 11.0


def _wacc_inputs(ticker: str) -> dict[str, float]:
    """Latest-FY income/debt line items for the WACC, straight from std facts."""
    ov = services.company_overview(ticker)
    if not ov.get("found"):
        return {}
    juris = ov["jurisdiction"]
    table = "fact_fundamentals_std_us" if juris == "US" else "fact_fundamentals_std_jp"
    eid_col = "cik" if juris == "US" else "edinet_code"
    eid = str(ov.get("cik")).zfill(10) if juris == "US" else ov.get("edinet_code")
    if not eid:
        return {}
    items = ["earnings_before_interest_taxes", "operating_income", "interest_expense",
             "earnings_before_taxes", "income_tax_provision", "total_financial_debt",
             "invested_capital", "net_income", "revenue"]
    df = read_sql(
        f"""
        SELECT DISTINCT ON (line_item_id) line_item_id, value::double precision AS value
        FROM {table}
        WHERE {eid_col} = %(eid)s AND fiscal_period IN ('FY','Annual')
          AND line_item_id = ANY(%(items)s) AND value IS NOT NULL
        ORDER BY line_item_id, fiscal_year DESC
        """,
        {"eid": eid, "items": items},
    )
    return {r["line_item_id"]: float(r["value"]) for r in df.to_dict("records")}


def segment_wacc(base: dict[str, Any], beta_delta: float = 0.0, growth_premium_bp: float = 0.0) -> float:
    """Segment-adjusted WACC: shift the equity beta and/or add a growth-risk premium.

    Higher-growth / higher-beta segments (e.g. cloud, AI) carry a higher discount
    rate than mature cash-cow segments. Used by the sum-of-the-parts DCF.
    """
    erp = base.get("equity_risk_premium_pct", _DEFAULT_ERP)
    re = base.get("cost_of_equity_pct", _DEFAULT_WACC)
    re_seg = re + beta_delta * erp
    we, wd = base.get("weight_equity", 1.0), base.get("weight_debt", 0.0)
    rd = base.get("cost_of_debt_pct", base.get("risk_free_pct", _DEFAULT_RF) + 1.0)
    t = base.get("tax_rate_pct", 21.0)
    wacc = we * re_seg + wd * rd * (1 - t / 100.0) + growth_premium_bp / 100.0
    return round(min(max(wacc, _MIN_WACC), _MAX_WACC), 2)


def _fact_map(packet: dict[str, Any]) -> dict[str, float]:
    """Latest-year line-item values from the modeled-statement snapshot."""
    rows = (packet.get("modeled_statements") or {}).get("rows") or []
    best: dict[str, tuple[int, float]] = {}
    for r in rows:
        li = r.get("line_item_id"); v = r.get("value"); fy = r.get("fiscal_year") or 0
        if li is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if li not in best or fy > best[li][0]:
            best[li] = (fy, v)
    return {k: v for k, (_, v) in best.items()}


def _latest_market_cap(packet: dict[str, Any], ticker: str) -> float | None:
    for r in (packet.get("market_metrics") or {}).get("rows") or []:
        if r.get("metric_id") == "market_capitalization" and r.get("value"):
            try:
                return float(r["value"])
            except (TypeError, ValueError):
                pass
    df = read_sql(
        "SELECT value FROM fact_market_metrics WHERE UPPER(ticker)=UPPER(%(t)s) "
        "AND metric_id='market_capitalization' AND value IS NOT NULL ORDER BY market_date DESC NULLS LAST LIMIT 1",
        {"t": ticker},
    )
    if not df.empty:
        try:
            return float(df.iloc[0]["value"])
        except (TypeError, ValueError):
            pass
    return None
