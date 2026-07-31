"""Market + fundamental time-series helpers for charts and the cash-flow story.

- ``price_history``: weekly-sampled closes for a ticker set (for the rebased
  relative-performance chart of the company vs its peers).
- ``financial_history``: multi-year revenue / capex / D&A / OCF / FCF series with
  derived ratios (capex % of revenue, capex/D&A, cash conversion) that power the
  cash-flow-and-capex narrative and history charts.
"""
from __future__ import annotations

from typing import Any

from .._db import read_sql

_HISTORY_ITEMS = (
    "revenue", "gross_profit",
    "earnings_before_interest_taxes_depreciation_amortization",
    "earnings_before_interest_taxes", "operating_income", "net_income",
    "earnings_before_taxes", "income_tax_provision",
    "cash_flow_from_operations", "capital_expenditures", "free_cash_flow",
    "depreciation_and_amortization_addback_cashflow", "depreciation",
    "stock_based_compensation", "stock_based_compensation_addback_cashflow",
    "share_repurchases", "dividends_paid", "invested_capital",
    "total_financial_debt", "net_debt", "total_equity",
)


def price_history(tickers: list[str], years: int = 3, sample_days: int = 5) -> dict[str, list[dict[str, Any]]]:
    """Weekly-sampled adjusted closes per ticker over the trailing window.

    Returns ``{ticker: [{"date": iso, "close": float}, ...]}`` sorted ascending.
    """
    if not tickers:
        return {}
    df = read_sql(
        """
        WITH ranked AS (
            SELECT ticker, date, COALESCE(adj_close, close) AS px,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date) AS rn
            FROM fact_prices_us
            WHERE UPPER(ticker) = ANY(%(tk)s)
              AND date >= (CURRENT_DATE - (%(yrs)s * INTERVAL '1 year'))
              AND COALESCE(adj_close, close) IS NOT NULL
        )
        SELECT ticker, date, px FROM ranked
        WHERE rn %% %(step)s = 0
        ORDER BY ticker, date
        """,
        {"tk": [t.upper() for t in tickers], "yrs": int(years), "step": int(sample_days)},
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in df.to_dict("records"):
        out.setdefault(str(r["ticker"]).upper(), []).append(
            {"date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
             "close": float(r["px"])}
        )
    return out


def financial_history(cik: str | None, jurisdiction: str, years: int = 6) -> list[dict[str, Any]]:
    """Per-fiscal-year cash-flow / income series with derived capex & conversion ratios."""
    if not cik:
        return []
    if jurisdiction == "US":
        table, join, entity = "fact_fundamentals_std_us", "s.cik = dc.cik", str(cik).zfill(10)
        eid_col = "cik"
    else:
        table, join, entity = "fact_fundamentals_std_jp", "s.edinet_code = dc.edinet_code", cik
        eid_col = "edinet_code"
    df = read_sql(
        f"""
        SELECT s.fiscal_year, s.line_item_id, s.value::double precision AS value
        FROM {table} s
        WHERE s.{eid_col} = %(eid)s
          AND s.fiscal_period IN ('FY','Annual')
          AND s.line_item_id = ANY(%(items)s)
          AND s.value IS NOT NULL
        """,
        {"eid": entity, "items": list(_HISTORY_ITEMS)},
    )
    if df.empty:
        return []
    by_year: dict[int, dict[str, float]] = {}
    for r in df.to_dict("records"):
        fy = int(r["fiscal_year"])
        by_year.setdefault(fy, {})[r["line_item_id"]] = float(r["value"])
    rows: list[dict[str, Any]] = []
    for fy in sorted(by_year)[-years:]:
        v = by_year[fy]
        rev = v.get("revenue")
        capex = abs(v.get("capital_expenditures")) if v.get("capital_expenditures") is not None else None
        da = v.get("depreciation_and_amortization_addback_cashflow") or v.get("depreciation")
        ocf = v.get("cash_flow_from_operations")
        fcf = v.get("free_cash_flow")
        if fcf is None and ocf is not None and capex is not None:
            fcf = ocf - capex
        ebitda = v.get("earnings_before_interest_taxes_depreciation_amortization")
        ebit = v.get("earnings_before_interest_taxes") or v.get("operating_income")
        pretax = v.get("earnings_before_taxes")
        tax = v.get("income_tax_provision")
        eff_tax = (abs(tax) / pretax) if (tax is not None and pretax and pretax > 0) else 0.21
        nopat = ebit * (1 - eff_tax) if ebit is not None else None
        ic = v.get("invested_capital")
        sbc = v.get("stock_based_compensation") or v.get("stock_based_compensation_addback_cashflow")
        buybacks = abs(v.get("share_repurchases")) if v.get("share_repurchases") is not None else None
        dividends = abs(v.get("dividends_paid")) if v.get("dividends_paid") is not None else None
        rows.append({
            "fiscal_year": fy,
            "revenue": rev,
            "ebitda": ebitda,
            "ebit": ebit,
            "nopat": nopat,
            "net_income": v.get("net_income"),
            "operating_cash_flow": ocf,
            "capex": capex,
            "d_a": da,
            "free_cash_flow": fcf,
            "sbc": sbc,
            "buybacks": buybacks,
            "dividends": dividends,
            "capital_return": (buybacks or 0) + (dividends or 0) if (buybacks or dividends) else None,
            "invested_capital": ic,
            "roic_pct": (nopat / ic * 100.0) if (nopat and ic) else None,
            "capex_pct_revenue": (capex / rev * 100.0) if (capex and rev) else None,
            "capex_to_ocf": (capex / ocf * 100.0) if (capex and ocf) else None,
            "capex_to_da": (capex / da) if (capex and da) else None,
            "fcf_margin": (fcf / rev * 100.0) if (fcf is not None and rev) else None,
            "fcf_after_sbc": (fcf - sbc) if (fcf is not None and sbc is not None) else None,
            "cash_conversion": (ocf / ebitda * 100.0) if (ocf and ebitda) else None,
        })
    return rows


def incremental_roic(hist: list[dict[str, Any]], wacc_pct: float, lookback: int = 3) -> dict[str, Any]:
    """Return on *incremental* invested capital over the recent window vs WACC.

    Incremental ROIC = Δ NOPAT / Δ Invested Capital across the window — the test of
    whether incremental reinvestment is earning above the cost of capital.
    """
    usable = [r for r in hist if r.get("nopat") is not None and r.get("invested_capital")]
    if len(usable) < 2:
        return {"available": False}
    window = usable[-(lookback + 1):] if len(usable) > lookback else usable
    first, last = window[0], window[-1]
    d_nopat = last["nopat"] - first["nopat"]
    d_ic = last["invested_capital"] - first["invested_capital"]
    inc_roic = (d_nopat / d_ic * 100.0) if d_ic else None
    return {
        "available": inc_roic is not None,
        "from_year": first["fiscal_year"], "to_year": last["fiscal_year"],
        "delta_nopat": d_nopat, "delta_invested_capital": d_ic,
        "incremental_roic_pct": round(inc_roic, 1) if inc_roic is not None else None,
        "wacc_pct": wacc_pct,
        "spread_vs_wacc_pct": round(inc_roic - wacc_pct, 1) if inc_roic is not None else None,
        "value_accretive": (inc_roic > wacc_pct) if inc_roic is not None else None,
        "latest_roic_pct": last.get("roic_pct"),
    }
