"""Relative valuation against the target's largest GICS peers.

The comparable-company set is selected deterministically from the warehouse:
same GICS sector (falling back to mapping sector only when GICS is unavailable or
too sparse), same jurisdiction first, and largest by latest market cap. No
hardcoded named-company universe is used.
"""
from __future__ import annotations

import statistics
from typing import Any

from .. import services
from .._db import read_sql

_MULT_KEYS = ("pe", "ev_ebitda", "ev_ebit", "ev_revenue", "ev_fcf", "pb", "fcf_yield")


def build_comps(ticker: str, packet: dict[str, Any] | None = None, sector_limit: int = 10) -> dict[str, Any]:
    overview = services.company_overview(ticker)
    if not overview.get("found"):
        return {"available": False}
    juris = overview["jurisdiction"]
    caps = _market_caps()

    target = _row(ticker, juris, caps)
    peer_packet = services.peer_group(ticker, limit=sector_limit) or {}
    peers = peer_packet.get("peers") or []
    for p in peers:
        _add_peg(p)
    _add_peg(target)

    peer_med = _medians(peers)
    implied = _implied_values(ticker, juris, peer_med, caps.get(ticker.upper()))

    return {
        "available": True,
        "target": target,
        "sector_peers": peers,
        "peer_median": peer_med,
        "implied": implied,
        "target_sector": peer_packet.get("target_sector") or overview.get("gics_sector_name") or overview.get("mapping_sector"),
        "selection_rule": peer_packet.get("selection_rule") or "same GICS sector; largest by latest market_capitalization",
    }


def _row(ticker: str, juris: str, caps: dict[str, float]) -> dict[str, Any]:
    cap = caps.get(ticker.upper())
    m = services._peer_metric_row(ticker, juris, cap)
    ov = services.company_overview(ticker)
    return {"ticker": ticker.upper(), "name": ov.get("name"), "market_cap": cap, **m}


def _add_peg(row: dict[str, Any]) -> None:
    pe, g = row.get("pe"), row.get("revenue_growth")
    # revenue_growth is stored as a fraction (0.15); PEG needs growth in percent.
    try:
        g_pct = g * 100.0 if (g is not None and abs(g) < 1.5) else g
        row["peg"] = round(pe / g_pct, 2) if (pe and g_pct and g_pct > 0) else None
    except (TypeError, ZeroDivisionError):
        row["peg"] = None


def _medians(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k in (*_MULT_KEYS, "peg", "revenue_growth", "ebitda_margin", "roic"):
        vals = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        out[k] = round(statistics.median(vals), 2) if vals else None
    return out


def _implied_values(ticker: str, juris: str, peer_med: dict, cap: float | None) -> dict[str, Any]:
    """Cross-check: apply GICS-peer median multiples to the company's own metrics."""
    std = "fact_fundamentals_std_us" if juris == "US" else "fact_fundamentals_std_jp"
    eid = "cik" if juris == "US" else "edinet_code"
    df = read_sql(
        f"""
        SELECT DISTINCT ON (s.line_item_id) s.line_item_id, s.value::double precision AS value
        FROM {std} s JOIN v_dim_company dc
          ON dc.jurisdiction=%(j)s AND s.{eid}=dc.{eid}
        WHERE UPPER(dc.ticker)=UPPER(%(t)s) AND s.fiscal_period IN ('FY','Annual')
          AND s.line_item_id = ANY(%(items)s) AND s.value IS NOT NULL
        ORDER BY s.line_item_id, s.fiscal_year DESC
        """,
        {"j": juris, "t": ticker,
         "items": ["earnings_before_interest_taxes_depreciation_amortization", "earnings_before_interest_taxes",
                   "net_income", "net_debt", "revenue", "free_cash_flow"]},
    )
    f = {r["line_item_id"]: float(r["value"]) for r in df.to_dict("records")}
    ebitda = f.get("earnings_before_interest_taxes_depreciation_amortization")
    ebit = f.get("earnings_before_interest_taxes")
    ni = f.get("net_income")
    fcf = f.get("free_cash_flow")
    nd = f.get("net_debt") or 0.0
    out: dict[str, Any] = {}
    out["peer_ev_ebitda_equity"] = (peer_med.get("ev_ebitda") * ebitda - nd) if (peer_med.get("ev_ebitda") and ebitda) else None
    out["peer_ev_ebit_equity"] = (peer_med.get("ev_ebit") * ebit - nd) if (peer_med.get("ev_ebit") and ebit) else None
    out["peer_ev_fcf_equity"] = (peer_med.get("ev_fcf") * fcf - nd) if (peer_med.get("ev_fcf") and fcf) else None
    out["peer_pe_equity"] = (peer_med.get("pe") * ni) if (peer_med.get("pe") and ni) else None
    out["current_market_cap"] = cap
    return out


def _market_caps() -> dict[str, float]:
    df = services._latest_market_caps()
    out: dict[str, float] = {}
    for r in df.to_dict("records"):
        try:
            out[str(r["ticker"]).upper()] = float(r["market_cap"])
        except (TypeError, ValueError):
            continue
    return out
