"""Trailing-twelve-months (TTM) aggregates and a quarterly-trend series.

Built from the standardized quarterly facts (`fact_fundamentals_std_us` /
`_jp`). Quarters are windowed by ``period_end`` — the reliable key — and the
un-stored fiscal-year-end quarter (Q4) is derived as ``FY − (Q1 + Q2 + Q3)`` for
each fiscal year. TTM is the sum of the four most recent *discrete* quarters.

Depends on the quarterly fiscal-year fix in ``std/us_standardize.py``
(``_period_align_quarterly_fiscal_year``); on data not yet re-standardized the
contiguity guard below returns ``available=False`` rather than a wrong TTM.
"""
from __future__ import annotations

from typing import Any

from .._db import read_sql

# Flow line items (summable across quarters). Mirrors marketdata._HISTORY_ITEMS.
_FLOW_ITEMS = (
    "revenue",
    "earnings_before_interest_taxes", "operating_income",
    "earnings_before_interest_taxes_depreciation_amortization",
    "net_income",
    "cash_flow_from_operations", "capital_expenditures", "free_cash_flow",
)
_FLOW_KEYS = ("revenue", "ebit", "ebitda", "net_income",
              "operating_cash_flow", "capex", "free_cash_flow")


def _entity(cik: str, jurisdiction: str) -> tuple[str, str, str]:
    if jurisdiction == "US":
        return "fact_fundamentals_std_us", "cik", str(cik).zfill(10)
    return "fact_fundamentals_std_jp", "edinet_code", cik


def _flows(d: dict[str, float]) -> dict[str, Any]:
    rev = d.get("revenue")
    ebit = d.get("earnings_before_interest_taxes")
    if ebit is None:
        ebit = d.get("operating_income")
    ocf = d.get("cash_flow_from_operations")
    capex = abs(d["capital_expenditures"]) if d.get("capital_expenditures") is not None else None
    fcf = d.get("free_cash_flow")
    if fcf is None and ocf is not None and capex is not None:
        fcf = ocf - capex
    return {
        "revenue": rev,
        "ebit": ebit,
        "ebitda": d.get("earnings_before_interest_taxes_depreciation_amortization"),
        "net_income": d.get("net_income"),
        "operating_cash_flow": ocf,
        "capex": capex,
        "free_cash_flow": fcf,
    }


def _margins(row: dict[str, Any]) -> dict[str, Any]:
    rev = row.get("revenue")
    def m(v: Any) -> Any:
        return (v / rev * 100.0) if (v is not None and rev) else None
    return {
        "ebit_margin_pct": m(row.get("ebit")),
        "fcf_margin_pct": m(row.get("free_cash_flow")),
        "net_margin_pct": m(row.get("net_income")),
    }


def _iso(d: Any) -> Any:
    return d.isoformat() if hasattr(d, "isoformat") else (str(d) if d is not None else None)


def quarterly_series(cik: str | None, jurisdiction: str, trend_quarters: int = 8) -> dict[str, Any]:
    """Return ``{available, quarters:[...trend...], ttm:{...}, latest_quarter_end}``.

    ``quarters`` are the last ``trend_quarters`` discrete quarters (oldest→newest)
    with per-quarter margins and YoY revenue growth. ``ttm`` sums the last 4.
    """
    if not cik:
        return {"available": False}
    table, eid_col, entity = _entity(cik, jurisdiction)
    df = read_sql(
        f"""
        SELECT s.fiscal_year, s.fiscal_period, s.period_end,
               s.line_item_id, s.value::double precision AS value
        FROM {table} s
        WHERE s.{eid_col} = %(eid)s
          AND s.fiscal_period IN ('FY','Annual','Q1','Q2','Q3','Q4')
          AND s.line_item_id = ANY(%(items)s)
          AND s.value IS NOT NULL
        """,
        {"eid": entity, "items": list(_FLOW_ITEMS)},
    )
    if df.empty:
        return {"available": False}

    cell: dict[tuple[int, str], dict[str, float]] = {}
    pend: dict[tuple[int, str], Any] = {}
    for r in df.to_dict("records"):
        key = (int(r["fiscal_year"]), str(r["fiscal_period"]))
        cell.setdefault(key, {})[r["line_item_id"]] = float(r["value"])
        pend[key] = r["period_end"]

    # Assemble discrete quarters: Q1–Q3 from stored rows; Q4 = FY − (Q1+Q2+Q3).
    quarters: list[dict[str, Any]] = []
    for fy in sorted({k[0] for k in cell}):
        for fp in ("Q1", "Q2", "Q3"):
            if (fy, fp) in cell:
                quarters.append({"fiscal_year": fy, "fiscal_period": fp,
                                 "period_end": pend[(fy, fp)], **_flows(cell[(fy, fp)])})
        fyk = (fy, "FY") if (fy, "FY") in cell else ((fy, "Annual") if (fy, "Annual") in cell else None)
        if fyk and all((fy, q) in cell for q in ("Q1", "Q2", "Q3")):
            fy_f = _flows(cell[fyk])
            q123 = [_flows(cell[(fy, q)]) for q in ("Q1", "Q2", "Q3")]
            q4: dict[str, Any] = {}
            for k in _FLOW_KEYS:
                fv = fy_f.get(k)
                parts = [q.get(k) for q in q123]
                q4[k] = (fv - sum(parts)) if (fv is not None and all(p is not None for p in parts)) else None
            quarters.append({"fiscal_year": fy, "fiscal_period": "Q4", "period_end": pend[fyk], **q4})

    quarters = [q for q in quarters if q.get("period_end") is not None]
    quarters.sort(key=lambda q: q["period_end"])
    if not quarters:
        return {"available": False}

    for q in quarters:
        q.update(_margins(q))
    for i, q in enumerate(quarters):
        prev = quarters[i - 4]["revenue"] if i >= 4 else None
        q["yoy_rev_growth_pct"] = ((q["revenue"] / prev - 1) * 100.0) if (prev and q.get("revenue") is not None) else None

    ttm = _ttm(quarters)
    trend = [{**q, "period_end": _iso(q["period_end"])} for q in quarters[-trend_quarters:]]
    return {
        "available": True,
        "quarters": trend,
        "ttm": ttm,
        "latest_quarter_end": _iso(quarters[-1]["period_end"]),
        "n_quarters": len(quarters),
    }


def _ttm(quarters: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the last four discrete quarters, with a contiguity guard so gappy /
    not-yet-re-standardized data yields ``available=False`` instead of a wrong TTM."""
    if len(quarters) < 4:
        return {"available": False}
    last4 = quarters[-4:]
    span_days = (last4[-1]["period_end"] - last4[0]["period_end"]).days
    if span_days > 400 or len({q["period_end"] for q in last4}) != 4:
        return {"available": False}  # not four consecutive quarters — don't trust the sum

    agg: dict[str, Any] = {}
    for k in _FLOW_KEYS:
        vals = [q.get(k) for q in last4]
        agg[k] = sum(vals) if all(v is not None for v in vals) else None
    if agg.get("revenue") is None:
        return {"available": False}
    return {
        "available": True,
        "period_end": _iso(last4[-1]["period_end"]),
        "start_period_end": _iso(last4[0]["period_end"]),
        **agg,
        **_margins(agg),
    }
