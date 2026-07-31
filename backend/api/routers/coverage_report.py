"""Coverage report — analyst-grade write-up generated from DB + DeepSeek.

`GET /api/coverage-report/{ticker}?jurisdiction=US|JP`

Steps:
  1. Pull deterministic data from the existing standardized fundamentals / metrics
     pipeline (the same tables the equities dashboard already uses).
  2. Compose a JSON "analytics packet" and hand it to DeepSeek.
  3. Ask the model for the narrative pieces (thesis, valuation prose, risks,
     rating, target price) in a strict JSON envelope.
  4. Merge deterministic numbers + LLM narrative into a single response that
     matches the frontend `Report` type the popout already renders.

24-hour in-process cache keyed by (ticker, jurisdiction); the endpoint accepts
?refresh=true to bypass.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

import llm_providers

from ..ai.llm_runtime import LLMError, chat_once, resolve_env_key
from ..db import acquire

router = APIRouter()
logger = logging.getLogger("mzqa.coverage_report")

_CACHE: dict[tuple[str, str], tuple[float, "CoverageReport"]] = {}
_TTL = 86400  # 24h

# Metric IDs we ask the standardized metrics table for. Any miss is tolerated
# (the LLM is instructed to acknowledge gaps).
_METRIC_IDS = [
    "revenue_growth_year_over_year",
    "revenue_compound_annual_growth_rate_5_year",
    "earnings_per_share_diluted_growth_year_over_year",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "return_on_assets",
    "debt_to_equity",
    "current_ratio",
    "free_cash_flow_yield",
]

# Raw line items used for the 5-year financial table.
_LINE_ITEMS = [
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "earnings_per_share_diluted",
    "cash_and_cash_equivalents",
    "total_debt",
]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ReportHighlight(BaseModel):
    label: str
    value: str
    sub: Optional[str] = None


class ReportFinancials(BaseModel):
    headers: list[str]
    rows: list[dict[str, Any]]    # {label, values: list[str]}


class ReportAnalyst(BaseModel):
    name: str
    title: str
    email: str


class CoverageReport(BaseModel):
    ticker: str
    jurisdiction: str
    company_name: str
    sector: Optional[str] = None
    industry: Optional[str] = None
    as_of: Optional[str] = None
    report_type: str
    rating: str
    target_price: str
    current_price: str
    upside: str
    one_line: str
    thesis: list[str]
    highlights: list[ReportHighlight]
    financials: ReportFinancials
    valuation: list[str]
    risks: list[str]
    analyst: ReportAnalyst
    firm: str
    source: Literal["llm", "fallback", "cache"] = "llm"
    generated_at: Optional[str] = None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _entity(conn, ticker: str, jurisdiction: str) -> dict:
    dim = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
    try:
        row = await conn.fetchrow(
            f"SELECT primary_ticker, name, "
            f"       COALESCE(gics_sector_name,'') AS sector, "
            f"       COALESCE(gics_industry_group_name,'') AS industry, "
            f"       COALESCE(exchange,'') AS exchange, "
            f"       COALESCE(country_code,'') AS country "
            f"FROM {dim} WHERE primary_ticker = $1 LIMIT 1",
            ticker,
        )
    except Exception:
        row = await conn.fetchrow(
            f"SELECT primary_ticker, name, "
            f"       COALESCE(gics_sector_name,'') AS sector, "
            f"       ''::text AS industry, "
            f"       COALESCE(exchange,'') AS exchange, "
            f"       ''::text AS country "
            f"FROM {dim} WHERE primary_ticker = $1 LIMIT 1",
            ticker,
        )
    out = dict(row) if row else {}
    # Best-effort currency from jurisdiction / country.
    if out:
        out["currency"] = "USD" if jurisdiction == "US" else "JPY"
    return out


async def _line_item_5y(conn, ticker: str, jurisdiction: str) -> dict:
    """Returns { line_item_id: { fy: value } } for the last 5 FYs.

    `fact_fundamentals_std_*` is keyed by CIK (US) or EDINET (JP) — not by
    ticker — so we resolve the entity identifier from `dim_company_*` first.
    """
    if jurisdiction == "US":
        tbl, dim, eid_col = "fact_fundamentals_std_us", "dim_company_us", "cik"
    else:
        tbl, dim, eid_col = "fact_fundamentals_std_jp", "dim_company_jp", "edinet_code"
    try:
        eid_row = await conn.fetchrow(
            f"SELECT {eid_col} FROM {dim} WHERE primary_ticker = $1 LIMIT 1",
            ticker,
        )
        if not eid_row or not eid_row[eid_col]:
            return {}
        eid = eid_row[eid_col]
        rows = await conn.fetch(
            f"""
            SELECT line_item_id, fiscal_year, value
            FROM   {tbl}
            WHERE  {eid_col} = $1
              AND  fiscal_period IN ('FY','Annual')
              AND  line_item_id = ANY($2)
              AND  value IS NOT NULL
              AND  fiscal_year >= (
                    SELECT MAX(fiscal_year) FROM {tbl}
                    WHERE {eid_col} = $1 AND fiscal_period IN ('FY','Annual')
                  ) - 4
            ORDER BY line_item_id, fiscal_year
            """,
            eid, _LINE_ITEMS,
        )
    except Exception as exc:
        logger.warning("coverage_report: line items failed (%s): %r", ticker, exc)
        return {}
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        out.setdefault(r["line_item_id"], {})[int(r["fiscal_year"])] = float(r["value"])
    return out


async def _metrics_5y(conn, ticker: str, jurisdiction: str) -> dict:
    tbl = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    try:
        rows = await conn.fetch(
            f"""
            SELECT metric_id, fiscal_year, value
            FROM   {tbl}
            WHERE  ticker = $1
              AND  fiscal_period IN ('FY','Annual')
              AND  metric_id = ANY($2)
              AND  value IS NOT NULL
              AND  fiscal_year >= (
                    SELECT MAX(fiscal_year) FROM {tbl}
                    WHERE ticker = $1 AND fiscal_period IN ('FY','Annual')
                  ) - 4
            ORDER BY metric_id, fiscal_year
            """,
            ticker, _METRIC_IDS,
        )
    except Exception as exc:
        logger.warning("coverage_report: metrics failed (%s): %r", ticker, exc)
        return {}
    out: dict[str, dict[int, float]] = {}
    for r in rows:
        out.setdefault(r["metric_id"], {})[int(r["fiscal_year"])] = float(r["value"])
    return out


async def _latest_price(conn, ticker: str, jurisdiction: str) -> dict:
    tbl = "sec.fact_prices_us" if jurisdiction == "US" else "sec.fact_prices_jp"
    try:
        row = await conn.fetchrow(
            f"SELECT COALESCE(adj_close, close) AS close, date FROM {tbl} "
            f"WHERE ticker = $1 AND COALESCE(adj_close, close) IS NOT NULL "
            f"ORDER BY date DESC LIMIT 1",
            ticker,
        )
        return dict(row) if row else {}
    except Exception as exc:
        logger.warning("coverage_report: price lookup failed (%s): %r", ticker, exc)
        return {}


# ---------------------------------------------------------------------------
# Payload + prompt
# ---------------------------------------------------------------------------

def _years_sorted(buckets: dict) -> list[int]:
    years: set[int] = set()
    for d in buckets.values():
        years |= set(d.keys())
    return sorted(years)


def _compose_payload(ent: dict, line: dict, met: dict, price: dict) -> dict:
    years = _years_sorted({**line, **met})[-5:]
    rows: list[dict] = []
    for li in _LINE_ITEMS:
        if li in line:
            rows.append({"line_item": li, "values_by_year": {str(y): line[li].get(y) for y in years}})
    metric_rows: list[dict] = []
    for mid in _METRIC_IDS:
        if mid in met:
            metric_rows.append({"metric_id": mid, "values_by_year": {str(y): met[mid].get(y) for y in years}})
    payload = {
        "company": {
            "ticker": ent.get("primary_ticker"),
            "name": ent.get("name"),
            "sector": ent.get("sector"),
            "industry": ent.get("industry"),
            "exchange": ent.get("exchange"),
            "currency": ent.get("currency"),
        },
        "years_covered": years,
        "raw_line_items": rows,
        "derived_metrics": metric_rows,
        "latest_price": price,
    }
    return payload


_SYSTEM_PROMPT = (
    "You are a senior sell-side equity research analyst at MZQA Securities, an institutional research house. "
    "Write tight, evidence-based commentary using ONLY the supplied analytics packet. Do not invent figures, "
    "macro views the packet does not support, or guidance you cannot derive from the numbers. When the packet "
    "is thin or empty, say so explicitly using 'data gap' phrasing in the relevant section. "
    "Style: declarative, banker's prose, German-bank cadence. Avoid superlatives. "
    "Return your output strictly as a JSON object matching the schema you are given — no markdown, no prose outside the JSON."
)


_USER_PROMPT_TEMPLATE = (
    "Write a coverage note for the company described in the analytics packet below.\n\n"
    "Return ONLY a JSON object with this shape (string fields, no markdown inside):\n"
    "{\n"
    '  "report_type": string,            // e.g. "Update", "Initiation", "Earnings Review"\n'
    '  "rating": "Buy" | "Hold" | "Sell",\n'
    '  "target_price": string,           // currency-prefixed; "—" if not derivable\n'
    '  "current_price": string,          // pull from latest_price; "—" if absent\n'
    '  "upside": string,                 // sign-prefixed pct vs current; "—" if not derivable\n'
    '  "one_line": string,               // <=24 words tagline, italics-worthy\n'
    '  "thesis": [string, ...],          // 5 bullets, each <=40 words, each cites a number from the packet\n'
    '  "highlights": [                   // exactly 4 KPI tiles\n'
    '    { "label": string, "value": string, "sub": string }\n'
    "  ],\n"
    '  "valuation": [string, string, string], // 3 paragraphs, ~3 sentences each\n'
    '  "risks": [string, string, string, string], // 4 bullets, <=30 words each\n'
    '  "analyst": { "name": "M. Offermann, CFA", "title": "Senior Analyst", "email": "research@mzqa-securities.com" },\n'
    '  "firm": "MZQA Securities"\n'
    "}\n\n"
    "Rules:\n"
    "- Use the currency in the packet when prefixing prices.\n"
    "- If derived_metrics is empty for revenue/EPS growth, state the gap inside the thesis bullet and use 'Hold' rating.\n"
    "- 'highlights' must be FOUR tiles using actual numbers from the packet (e.g. latest revenue, latest margin, 5y CAGR, free-cash-flow yield).\n"
    "- 'valuation' must reference at least one concrete multiple (e.g. EV/EBITDA, P/E) when the packet supports it.\n"
    "- Keep each thesis / risk bullet on a single line.\n\n"
    "Analytics packet:\n"
)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

async def _generate_narrative(payload: dict) -> dict:
    prov = llm_providers.get(None)   # AI_ANALYST_LLM_PROVIDER, else DeepSeek
    api_key = resolve_env_key(prov.id)
    if not api_key:
        raise LLMError(f"{prov.env[0]} is not configured.")
    model = llm_providers.chat_model(prov.id, os.environ.get("DEEPSEEK_MODEL"))
    user = _USER_PROMPT_TEMPLATE + json.dumps(payload, default=str)
    msg = await chat_once(
        api_key=api_key,
        provider=prov.id,
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.25,
        max_tokens=2200,
        response_format={"type": "json_object"},
    )
    content = (msg.get("content") or "").strip()
    if not content:
        raise LLMError("Empty content from DeepSeek.")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        # Trim any accidental markdown fence
        cleaned = content.strip().lstrip("`").lstrip()
        cleaned = cleaned.split("```", 1)[0] if "```" in cleaned else cleaned
        try:
            return json.loads(cleaned)
        except Exception:
            raise LLMError(f"Could not parse DeepSeek JSON: {exc}: {content[:240]}")


# ---------------------------------------------------------------------------
# Fallback builder when LLM is unavailable / fails
# ---------------------------------------------------------------------------

def _fallback_report(ent: dict, line: dict, met: dict, price: dict) -> dict:
    years = _years_sorted({**line, **met})
    last = years[-1] if years else None
    rev = (line.get("revenue") or {}).get(last) if last else None
    ni = (line.get("net_income") or {}).get(last) if last else None
    ccy = ent.get("currency") or ("USD" if ent.get("exchange") in ("NASDAQ","NYSE","NYSEAMER") else "")
    def fmt_money(v):
        if v is None: return "—"
        v = float(v)
        a = abs(v)
        if a >= 1e12: return f"{ccy} {v/1e12:.2f}T"
        if a >= 1e9:  return f"{ccy} {v/1e9:.1f}B"
        if a >= 1e6:  return f"{ccy} {v/1e6:.1f}M"
        return f"{ccy} {v:,.0f}"
    return {
        "report_type": "Coverage update — DB only",
        "rating": "Hold",
        "target_price": "—",
        "current_price": (f"{ccy} {price.get('close'):.2f}" if price.get("close") else "—"),
        "upside": "—",
        "one_line": "Database-only summary — narrative model unavailable; figures sourced from the standardized fundamentals pipeline.",
        "thesis": [
            f"Coverage of {ent.get('name') or ent.get('primary_ticker')} maintained on standardized fundamentals; rating placeholder pending narrative refresh.",
            f"Latest FY{last}: revenue {fmt_money(rev)}, net income {fmt_money(ni)}." if last else "No fiscal-year data found in the standardized fundamentals table.",
            "Derived margin and growth metrics drawn from fact_metrics_* (see highlights).",
            "Investment view will be re-stated once the LLM narrative pipeline is reconnected.",
        ],
        "highlights": [
            {"label": f"FY{last} revenue", "value": fmt_money(rev)} if last else {"label": "Revenue", "value": "—"},
            {"label": f"FY{last} net income", "value": fmt_money(ni)} if last else {"label": "Net income", "value": "—"},
            {"label": "Years available", "value": str(len(years))},
            {"label": "Metric coverage", "value": f"{len(met)} of {len(_METRIC_IDS)}"},
        ],
        "valuation": [
            "Valuation work is currently parked. Once the DeepSeek key is configured, the next refresh will include a DCF cross-check and a multiple-based reference range against the company's GICS peer set.",
            "Until then, treat any displayed target price as informational only.",
        ],
        "risks": [
            "Stale data risk — the standardized fundamentals snapshot updates on the regular ingestion schedule.",
            "Narrative-generation risk — model output unavailable until DEEPSEEK_API_KEY is configured.",
            "Cross-currency comparability not applied in this fallback view.",
            "Sector/macro context is omitted in this minimal report.",
        ],
        "analyst": {"name": "Coverage team", "title": "MZQA Equity Research", "email": "research@mzqa-securities.com"},
        "firm": "MZQA Securities",
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/ping")
async def coverage_report_ping_early() -> dict:
    return {"ok": True, "version": "2026-06-04-B", "has_api_key": bool(resolve_env_key())}


@router.get("/{ticker}", response_model=CoverageReport)
async def coverage_report(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query("US"),
    refresh: bool = Query(False),
) -> CoverageReport:
    key = (ticker.upper(), jurisdiction)
    if not refresh:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.time():
            cached[1].source = "cache"
            return cached[1]

    async with acquire() as conn:
        ent = await _entity(conn, ticker, jurisdiction)
        if not ent or not ent.get("primary_ticker"):
            raise HTTPException(status_code=404, detail=f"{ticker} not found in dim_company_{jurisdiction.lower()}")
        line = await _line_item_5y(conn, ticker, jurisdiction)
        met = await _metrics_5y(conn, ticker, jurisdiction)
        price = await _latest_price(conn, ticker, jurisdiction)

    payload = _compose_payload(ent, line, met, price)

    # Build the financial table from raw line items, regardless of LLM availability.
    years = payload["years_covered"][-5:]
    def fmt_row(li_id: str) -> dict:
        d = (line.get(li_id) or {})
        values: list[str] = []
        for y in years:
            v = d.get(y)
            if v is None:
                values.append("—")
            elif li_id == "earnings_per_share_diluted":
                values.append(f"{v:.2f}")
            else:
                a = abs(float(v))
                if a >= 1e9: values.append(f"{v/1e9:.1f}")
                elif a >= 1e6: values.append(f"{v/1e6:.1f}")
                else: values.append(f"{v:,.0f}")
        return {"label": li_id.replace("_"," ").title(), "values": values}
    financials_rows = [fmt_row(li) for li in _LINE_ITEMS if li in line]
    financials = ReportFinancials(
        headers=[f"FY{y}" for y in years],
        rows=financials_rows,
    )

    source: str = "llm"
    try:
        narr = await _generate_narrative(payload)
    except LLMError as exc:
        logger.warning("coverage_report: LLM unavailable — using fallback (%s): %s", ticker, exc)
        narr = _fallback_report(ent, line, met, price)
        source = "fallback"
    except Exception as exc:
        logger.exception("coverage_report: unexpected LLM error for %s: %s", ticker, exc)
        narr = _fallback_report(ent, line, met, price)
        source = "fallback"

    def _coerce_highlights(items) -> list[ReportHighlight]:
        out: list[ReportHighlight] = []
        for h in items or []:
            try:
                out.append(ReportHighlight(
                    label=str(h.get("label","")),
                    value=str(h.get("value","—")),
                    sub=(str(h["sub"]) if h.get("sub") not in (None,"") else None),
                ))
            except Exception:
                continue
        return out[:4]

    report = CoverageReport(
        ticker=ticker.upper(),
        jurisdiction=jurisdiction,
        company_name=ent.get("name") or ticker,
        sector=ent.get("sector") or None,
        industry=ent.get("industry") or None,
        as_of=(str(price.get("date")) if price.get("date") else None),
        report_type=str(narr.get("report_type") or "Coverage Update"),
        rating=str(narr.get("rating") or "Hold"),
        target_price=str(narr.get("target_price") or "—"),
        current_price=str(narr.get("current_price") or "—"),
        upside=str(narr.get("upside") or "—"),
        one_line=str(narr.get("one_line") or ""),
        thesis=[str(x) for x in (narr.get("thesis") or [])][:6],
        highlights=_coerce_highlights(narr.get("highlights")),
        financials=financials,
        valuation=[str(x) for x in (narr.get("valuation") or [])][:4],
        risks=[str(x) for x in (narr.get("risks") or [])][:6],
        analyst=ReportAnalyst(
            name=str((narr.get("analyst") or {}).get("name") or "MZQA Equity Research"),
            title=str((narr.get("analyst") or {}).get("title") or "Coverage team"),
            email=str((narr.get("analyst") or {}).get("email") or "research@mzqa-securities.com"),
        ),
        firm=str(narr.get("firm") or "MZQA Securities"),
        source=source,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    _CACHE[key] = (time.time() + _TTL, report)
    return report


