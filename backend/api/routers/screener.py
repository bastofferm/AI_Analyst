"""Screener endpoints.

Three routes:

* `GET  /api/screener/meta` - filter-key catalogue (label, unit, type). Shared
  between the structured filter rail and the AI prompt's allowed-keys list.
* `POST /api/screener/run`  - deterministic structured screen. Joins the
  per-ticker latest-FY metrics in `fact_metrics_us` / `fact_metrics_jp` against
  the company dimension, applies range filters with predicate pushdown, sorts,
  and returns up to `limit` rows (server-capped at 500).
* `POST /api/screener/ai`   - natural-language -> structured filter object.
  Calls DeepSeek with a JSON-forced response and validates the output against
  the same schema as `/run`. Does not run the screen; the frontend calls
  `/run` with the returned filters so the user sees what the AI inferred.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime
from ..db import acquire


router = APIRouter()
logger = logging.getLogger("mzqa.screener")


# ---------------------------------------------------------------------------
# Filter catalogue
# ---------------------------------------------------------------------------
#
# Each entry maps a stable screener filter key to a metric id in
# fact_metrics_us / fact_metrics_jp (or "market_cap" for the dimension table
# join). The catalogue is consumed by both the frontend filter rail and the
# AI prompt as the allowed-keys list.

class FilterDef(BaseModel):
    key: str
    label: str
    group: Literal["valuation", "profitability", "growth", "size"]
    metric_id: Optional[str] = None
    unit: Literal["ratio", "pct", "currency"]
    decimals: int = 2
    suggested_min: float | None = None
    suggested_max: float | None = None


FILTER_CATALOGUE: list[FilterDef] = [
    FilterDef(key="market_cap_usd", label="Market cap (USD)", group="size",
              metric_id="market_capitalization", unit="currency", decimals=0),
    FilterDef(key="pe",             label="P/E (trailing)",   group="valuation",
              metric_id="price_to_earnings_trailing", unit="ratio",
              suggested_min=0, suggested_max=40),
    FilterDef(key="pb",             label="P/B",              group="valuation",
              metric_id="price_to_book", unit="ratio",
              suggested_min=0, suggested_max=10),
    FilterDef(key="ev_ebitda",      label="EV / EBITDA",      group="valuation",
              metric_id="enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
              unit="ratio", suggested_min=0, suggested_max=30),
    FilterDef(key="fcf_yield",      label="FCF yield",        group="valuation",
              metric_id="free_cash_flow_yield", unit="pct",
              suggested_min=0, suggested_max=0.25),
    FilterDef(key="dividend_yield", label="Dividend yield",   group="valuation",
              metric_id="dividend_yield", unit="pct",
              suggested_min=0, suggested_max=0.10),
    FilterDef(key="gross_margin",   label="Gross margin",     group="profitability",
              metric_id="gross_margin", unit="pct",
              suggested_min=0, suggested_max=1.0),
    FilterDef(key="operating_margin", label="Operating margin", group="profitability",
              metric_id="operating_margin", unit="pct",
              suggested_min=-0.2, suggested_max=0.6),
    FilterDef(key="rev_yoy",        label="Revenue YoY",      group="growth",
              metric_id="revenue_growth_year_over_year", unit="pct",
              suggested_min=-0.5, suggested_max=1.0),
    FilterDef(key="rev_cagr_3y",    label="Revenue CAGR 3Y",  group="growth",
              metric_id="revenue_compound_annual_growth_rate_3_year", unit="pct",
              suggested_min=-0.2, suggested_max=0.5),
]


_CAT_BY_KEY: dict[str, FilterDef] = {f.key: f for f in FILTER_CATALOGUE}


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------

class Range(BaseModel):
    min: float | None = None
    max: float | None = None


class Universe(BaseModel):
    jurisdiction: Literal["US", "JP", "INTL"] = "US"
    country_code: str | None = None   # optional ISO-2 filter; only applied when jurisdiction=INTL
    region: str | None = None         # optional INTL region (e.g. "Europe"); filters dim_company_intl.region
    exchanges: list[str] | None = None
    sectors:   list[str] | None = None      # GICS sector codes (e.g. "45")
    industries: list[str] | None = None     # GICS industry-group codes
    portfolio_tickers: list[str] | None = None  # restricts the universe; takes precedence


class Sort(BaseModel):
    key: str = "market_cap_usd"
    dir: Literal["asc", "desc"] = "desc"


class ScreenerRunRequest(BaseModel):
    universe: Universe = Universe()
    filters: dict[str, Range] = Field(default_factory=dict)
    sort: Sort = Sort()
    limit: int = Field(default=100, ge=1, le=500)


def _logo_id(entity_id: str | None, jurisdiction: str) -> str | None:
    """Filename stem of the internal company logo, or None when we have none.

    Mirrors company.py: the shared MZQA logo library names US files by
    zero-padded CIK and JP files by EDINET code. INTL has no coverage, so those
    resolve to None and the UI falls back to the external CDN / an initials tile.
    """
    if not entity_id or jurisdiction == "INTL":
        return None
    eid = str(entity_id).strip()
    if not eid:
        return None
    return eid.zfill(10) if jurisdiction == "US" else eid


class ScreenerRow(BaseModel):
    ticker: str
    name: str
    jurisdiction: Literal["US", "JP", "INTL"]
    sector: str | None = None
    metrics: dict[str, float | None]
    # CIK (US, zero-padded) or EDINET code (JP); None for INTL — GET /logos/{id}.
    logo_id: str | None = None


class ScreenerRunResponse(BaseModel):
    rows: list[ScreenerRow]
    total_matched: int
    applied_filters: dict[str, Range]
    applied_universe: Universe
    applied_sort: Sort


class ScreenerAiRequest(BaseModel):
    prompt: str
    jurisdiction: Literal["US", "JP", "INTL"] = "US"
    portfolio_tickers: list[str] | None = None
    provider: str | None = None      # llm_providers id; None -> server default (DeepSeek)
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class ScreenerAiResponse(BaseModel):
    filters: dict[str, Range]
    universe: Universe
    sort: Sort
    rationale: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ScreenerPromptPreviewRequest(BaseModel):
    label: str
    prompt: str
    jurisdiction: Literal["US", "JP", "INTL"] = "US"
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class ScreenerPromptPreviewResponse(BaseModel):
    label: str
    prompt: str
    summary: str | None = None
    rows: list[ScreenerRow] = Field(default_factory=list)
    ai_filters: ScreenerAiResponse | None = None
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /meta
# ---------------------------------------------------------------------------

@router.get("/meta")
async def screener_meta() -> dict:
    return {
        "filters": [f.model_dump() for f in FILTER_CATALOGUE],
        "groups": ["valuation", "profitability", "growth", "size"],
        "sort_keys": [f.key for f in FILTER_CATALOGUE],
    }


# INTL region buckets, matching dim_company_intl.region (the /markets endpoint
# groups by the same column). Used to validate the region the AI screener infers.
_INTL_REGIONS: tuple[str, ...] = ("Europe", "Asia", "Africa", "North America", "South America", "MENA")
_REGION_ALIASES: dict[str, str] = {
    "europe": "Europe",
    "asia": "Asia",
    "africa": "Africa",
    "north america": "North America",
    "south america": "South America",
    "latin america": "South America",
    "latam": "South America",
    "mena": "MENA",
    "middle east": "MENA",
}


def _canonical_region(raw: str) -> str | None:
    """Map an LLM region string to the exact dim_company_intl.region label, or None."""
    s = raw.strip().lower()
    for r in _INTL_REGIONS:
        if s == r.lower():
            return r
    return _REGION_ALIASES.get(s)


# Static country_name overrides for INTL codes. dim_company_intl has multiple
# country_name values per code (Yahoo noise); the dropdown wants one canonical label.
_INTL_COUNTRY_NAMES: dict[str, str] = {
    "KR": "South Korea", "SG": "Singapore", "CN": "China (mainland)", "HK": "Hong Kong",
    "IN": "India", "TH": "Thailand", "ID": "Indonesia", "MY": "Malaysia", "PH": "Philippines",
    "TW": "Taiwan", "VN": "Vietnam", "IL": "Israel", "AE": "UAE", "SA": "Saudi Arabia",
    "GB": "United Kingdom", "DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
    "NL": "Netherlands", "BE": "Belgium", "SE": "Sweden", "FI": "Finland", "CH": "Switzerland",
    "NO": "Norway", "DK": "Denmark", "IS": "Iceland", "AT": "Austria", "IE": "Ireland",
    "PL": "Poland", "CZ": "Czech Republic", "HU": "Hungary", "PT": "Portugal", "GR": "Greece",
    "TR": "Turkey", "RU": "Russia",
    "CA": "Canada", "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "CL": "Chile",
    "AU": "Australia", "NZ": "New Zealand", "ZA": "South Africa",
}


@router.get("/markets")
async def screener_markets() -> dict:
    """Return the two-tier market picker: US, JP, and one entry per INTL region
    with the countries it contains (from dim_company_intl.country_code)."""
    try:
        async with acquire() as conn:
            us_count = await conn.fetchval(
                "SELECT COUNT(*) FROM dim_company_us "
                "WHERE primary_ticker IS NOT NULL AND COALESCE(include_in_pipeline, true)"
            )
            jp_count = await conn.fetchval(
                "SELECT COUNT(*) FROM dim_company_jp "
                "WHERE primary_ticker IS NOT NULL AND COALESCE(include_in_pipeline, true)"
            )
            rows = await conn.fetch(
                """
                SELECT region, country_code, COUNT(*)::int AS n
                FROM   dim_company_intl
                WHERE  primary_ticker IS NOT NULL
                  AND  COALESCE(include_in_pipeline, true)
                  AND  country_code IS NOT NULL AND country_code <> ''
                GROUP  BY region, country_code
                ORDER  BY region NULLS LAST, n DESC
                """
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Market lookup failed: {exc}") from exc

    # Aggregate by (region, country_code) — noise-suppress by picking the canonical
    # name from _INTL_COUNTRY_NAMES, ignoring dim_company_intl.country_name which is
    # dirty (e.g. .GB rows tagged "Jersey", "Bermuda").
    intl_regions: dict[str, dict] = {}
    for r in rows:
        region = (r["region"] or "Other").strip() or "Other"
        cc = str(r["country_code"]).upper()
        name = _INTL_COUNTRY_NAMES.get(cc, cc)
        bucket = intl_regions.setdefault(region, {"label": region, "countries": {}})
        prev = bucket["countries"].get(cc)
        if prev is None:
            bucket["countries"][cc] = {"code": cc, "name": name, "count": int(r["n"])}
        else:
            prev["count"] += int(r["n"])

    intl_out: list[dict] = []
    _REGION_ORDER = ["Europe", "Asia", "Americas", "Africa", "MENA", "Other"]
    for region_label in sorted(intl_regions.keys(),
                                key=lambda r: (_REGION_ORDER.index(r) if r in _REGION_ORDER else 99, r)):
        bucket = intl_regions[region_label]
        countries = sorted(bucket["countries"].values(), key=lambda c: -c["count"])
        intl_out.append({
            "region": region_label,
            "total": sum(c["count"] for c in countries),
            "countries": countries,
        })

    return {
        "primary": [
            {"jurisdiction": "US", "label": "United States", "count": int(us_count or 0)},
            {"jurisdiction": "JP", "label": "Japan", "count": int(jp_count or 0)},
        ],
        "intl_regions": intl_out,
    }


# ---------------------------------------------------------------------------
# /run
# ---------------------------------------------------------------------------

def _validate_filters(filters: dict[str, Range]) -> dict[str, Range]:
    """Drop unknown keys and empty ranges; return cleaned filters."""
    out: dict[str, Range] = {}
    for k, r in filters.items():
        if k not in _CAT_BY_KEY:
            continue
        if r.min is None and r.max is None:
            continue
        out[k] = r
    return out


@router.post("/run", response_model=ScreenerRunResponse)
async def screener_run(req: ScreenerRunRequest) -> ScreenerRunResponse:
    juris = req.universe.jurisdiction
    filters = _validate_filters(req.filters)

    # Sort key must be in catalogue; otherwise default.
    sort_key = req.sort.key if req.sort.key in _CAT_BY_KEY else "market_cap_usd"
    sort_dir = req.sort.dir
    sort_def = _CAT_BY_KEY[sort_key]

    # Build the metric-pivot CTE. We pull values for every requested filter +
    # the sort key + a small fixed display set so the response is informative.
    display_keys = ["market_cap_usd", "pe", "pb", "ev_ebitda", "fcf_yield", "rev_yoy"]
    metric_keys = list({*filters.keys(), sort_key, *display_keys})
    metric_defs = [_CAT_BY_KEY[k] for k in metric_keys]

    # Per-ticker latest-FY metric value. Three-way dispatch for US / JP / INTL.
    fact_table = {"US": "fact_metrics_us", "JP": "fact_metrics_jp", "INTL": "fact_metrics_intl"}[juris]
    metric_ids_no_mcap = [d.metric_id for d in metric_defs if d.key != "market_cap_usd" and d.metric_id]

    # SQL: dim filtered by universe, joined to latest FY metric per metric_id,
    # pivoted into columns. We always join market_cap from fact_market_metrics
    # so size / market_cap_usd filters work.
    where_dim = ["primary_ticker IS NOT NULL", "COALESCE(include_in_pipeline, true)"]
    args: list[Any] = []

    def _arg(v: Any) -> str:
        args.append(v)
        return f"${len(args)}"

    if req.universe.portfolio_tickers:
        where_dim.append(f"primary_ticker = ANY({_arg(req.universe.portfolio_tickers)}::text[])")
    if req.universe.sectors:
        # INTL now carries GICS codes (backfilled from Yahoo sector/industry), so the
        # filter is strict for every jurisdiction. Rows still lacking a code (ETFs,
        # unclassified Yahoo names) are correctly excluded when a sector is selected.
        where_dim.append(f"gics_sector_code = ANY({_arg(req.universe.sectors)}::text[])")
    if req.universe.industries:
        where_dim.append(f"gics_industry_group_code = ANY({_arg(req.universe.industries)}::text[])")
    if req.universe.exchanges and juris in ("US", "INTL"):
        where_dim.append(f"exchange = ANY({_arg(req.universe.exchanges)}::text[])")
    if juris == "INTL" and req.universe.region:
        # Region bucket from dim_company_intl.region (e.g. "Europe", "Africa"). The
        # dropdown/LLM sends the same stripped label the /markets endpoint groups by,
        # so btrim() both sides to survive stray whitespace in the warehouse.
        where_dim.append(f"btrim(region) = {_arg(req.universe.region.strip())}")
    if juris == "INTL" and req.universe.country_code:
        # Two-letter ISO code from dim_company_intl.country_code (dropdown filter).
        where_dim.append(f"country_code = {_arg(req.universe.country_code.upper())}")

    dim_table = {"US": "dim_company_us", "JP": "dim_company_jp", "INTL": "dim_company_intl"}[juris]
    name_expr = "name" if juris == "US" else "COALESCE(name_en, name, primary_ticker)"

    # Pivot CTE - one column per metric_id.
    pivot_cols_sql = []
    for d in metric_defs:
        if d.key == "market_cap_usd":
            continue  # joined separately from fact_market_metrics
        pivot_cols_sql.append(
            f"MAX(CASE WHEN metric_id = {_arg(d.metric_id)} THEN value END) AS \"{d.key}\""
        )
    pivot_sql = ", ".join(pivot_cols_sql) if pivot_cols_sql else "NULL::numeric AS _dummy"

    metric_ids_arg = _arg(metric_ids_no_mcap) if metric_ids_no_mcap else None

    metric_join_sql = ""
    if metric_ids_arg is not None:
        metric_join_sql = f"""
            LEFT JOIN LATERAL (
                SELECT {pivot_sql}
                FROM (
                    -- Resolve each metric_id independently, preferring a trailing-twelve-month
                    -- row over the annual one. `IN ('FY','TTM')` is a closed allowlist, NOT a
                    -- loosening: fact_metrics_us/_jp also hold Q1..Q4/H1 rows whose values are
                    -- per-quarter and misleading (a Q2 P/E is ~4x too high), and those stay
                    -- excluded exactly as before. Only fact_metrics_intl has TTM rows today, so
                    -- for US/JP the IN is row-identical to the old `= 'FY'` and the TTM sort key
                    -- is a constant false — i.e. provably the same output.
                    -- The TTM preference must be EXPLICIT: left to `fiscal_year DESC,
                    -- period_end DESC` the FY/TTM choice would silently hinge on each company's
                    -- fiscal calendar.
                    SELECT DISTINCT ON (metric_id) metric_id, value
                    FROM   {fact_table}
                    WHERE  ticker = d.primary_ticker
                      AND  fiscal_period IN ('FY', 'TTM')
                      AND  metric_id = ANY({metric_ids_arg}::text[])
                    ORDER  BY metric_id,
                              (fiscal_period = 'TTM') DESC,
                              fiscal_year DESC,
                              period_end DESC NULLS LAST
                ) latest
            ) m ON true
        """

    # Market cap from fact_market_metrics. JP coverage can lag the market-metric
    # sidecar, so fall back to latest close * filed shares from the JP dimension.
    if juris == "JP":
        mcap_join_sql = f"""
            LEFT JOIN LATERAL (
                SELECT COALESCE(
                    (
                        SELECT value
                        FROM   fact_market_metrics
                        WHERE  jurisdiction = {_arg(juris)}
                          AND  metric_id    = 'market_capitalization'
                          AND  (
                              entity_id = d.edinet_code
                              OR ticker = d.primary_ticker
                              OR ticker = regexp_replace(d.primary_ticker, '\\.T$', '')
                          )
                        ORDER  BY market_date DESC NULLS LAST, period_end DESC NULLS LAST
                        LIMIT  1
                    ),
                    (
                        SELECT p.close * NULLIF(d.shares_outstanding, 0)
                        FROM   fact_prices_jp p
                        WHERE  p.ticker = regexp_replace(d.primary_ticker, '\\.T$', '')
                          AND  p.close IS NOT NULL
                        ORDER  BY p.date DESC
                        LIMIT  1
                    )
                ) AS market_cap_usd
            ) mc ON true
        """
    elif juris == "INTL":
        mcap_join_sql = f"""
            LEFT JOIN LATERAL (
                SELECT value AS market_cap_usd
                FROM   fact_market_metrics
                WHERE  jurisdiction = 'INTL'
                  AND  entity_id    = d.intl_company_id::text
                  AND  metric_id    = 'market_capitalization'
                ORDER  BY market_date DESC NULLS LAST, period_end DESC NULLS LAST
                LIMIT  1
            ) mc ON true
        """
    else:
        mcap_join_sql = f"""
            LEFT JOIN LATERAL (
                SELECT value AS market_cap_usd
                FROM   fact_market_metrics
                WHERE  jurisdiction = {_arg(juris)}
                  AND  entity_id    = d.cik::text
                  AND  metric_id    = 'market_capitalization'
                ORDER  BY market_date DESC NULLS LAST, period_end DESC NULLS LAST
                LIMIT  1
            ) mc ON true
        """

    # Build filter WHERE clauses against the pivoted columns.
    metric_where: list[str] = []
    for k, r in filters.items():
        col = f"\"{k}\"" if k != "market_cap_usd" else "market_cap_usd"
        src = "m." if k != "market_cap_usd" else "mc."
        # When the user constrains a metric, require it to be non-null - rows
        # with missing values cannot prove they pass the predicate.
        metric_where.append(f"{src}{col} IS NOT NULL")
        if r.min is not None:
            metric_where.append(f"{src}{col} >= {_arg(r.min)}")
        if r.max is not None:
            metric_where.append(f"{src}{col} <= {_arg(r.max)}")

    where_clauses = " AND ".join(where_dim + metric_where)

    # ORDER BY runs against the outer CTE, where every projected column is
    # unqualified (e.g. `pe`, `market_cap_usd`). Prefixing with the inner
    # LATERAL aliases (`m.`, `mc.`) raises "missing FROM-clause entry".
    sort_col = "market_cap_usd" if sort_key == "market_cap_usd" else f"\"{sort_key}\""
    nulls = "NULLS LAST" if sort_dir == "desc" else "NULLS FIRST"
    limit_arg = _arg(req.limit)

    # Entity id used to resolve the internal /logos/{id} asset (US: CIK, JP:
    # EDINET). INTL has no logo-library coverage, so it stays NULL.
    entity_id_expr = "d.cik::text" if juris == "US" else "d.edinet_code" if juris == "JP" else "NULL::text"

    sql = f"""
        WITH candidates AS (
            SELECT d.primary_ticker AS ticker,
                   {name_expr} AS name,
                   d.gics_sector_code AS sector_code,
                   {entity_id_expr} AS entity_id,
                   mc.market_cap_usd
                   {', ' + ', '.join(f'm."{d.key}"' for d in metric_defs if d.key != 'market_cap_usd') if metric_ids_no_mcap else ''}
            FROM   {dim_table} d
            {mcap_join_sql}
            {metric_join_sql}
            WHERE  {where_clauses}
        ), counted AS (
            SELECT *, COUNT(*) OVER () AS total_matched
            FROM   candidates
        )
        SELECT *
        FROM   counted
        ORDER  BY {sort_col} {sort_dir.upper()} {nulls}
        LIMIT  {limit_arg}
    """

    logger.info("screener run: juris=%s filters=%s sort=%s limit=%d",
                juris, list(filters.keys()), f"{sort_key} {sort_dir}", req.limit)

    try:
        async with acquire() as conn:
            rows = await conn.fetch(sql, *args)
    except Exception as exc:
        logger.exception("screener run failed")
        raise HTTPException(status_code=500, detail=f"Screener query failed: {exc}") from exc

    total_matched = int(rows[0]["total_matched"]) if rows else 0
    out_rows: list[ScreenerRow] = []
    for r in rows:
        metrics_map: dict[str, float | None] = {}
        for d in metric_defs:
            col = "market_cap_usd" if d.key == "market_cap_usd" else d.key
            v = r.get(col) if hasattr(r, "get") else r[col] if col in r.keys() else None
            try:
                metrics_map[d.key] = float(v) if v is not None else None
            except (TypeError, ValueError):
                metrics_map[d.key] = None
        out_rows.append(ScreenerRow(
            ticker=r["ticker"],
            name=r["name"] or r["ticker"],
            jurisdiction=juris,
            sector=r["sector_code"],
            metrics=metrics_map,
            logo_id=_logo_id(r["entity_id"], juris),
        ))

    return ScreenerRunResponse(
        rows=out_rows,
        total_matched=total_matched,
        applied_filters=filters,
        applied_universe=req.universe,
        applied_sort=Sort(key=sort_key, dir=sort_dir),
    )


# ---------------------------------------------------------------------------
# /ai - prompt -> structured filter object
# ---------------------------------------------------------------------------

def _ai_system_prompt() -> str:
    keys_doc = "\n".join(
        f"- `{f.key}` ({f.unit}, group={f.group}): {f.label}"
        + (f" — typical range {f.suggested_min}-{f.suggested_max}"
           if f.suggested_min is not None and f.suggested_max is not None else "")
        for f in FILTER_CATALOGUE
    )
    return f"""You translate a natural-language stock-screening request into a structured filter object.

Allowed filter keys (use exactly these keys; ignore other concepts you cannot map):
{keys_doc}

Units: pct values are decimal (e.g. 0.15 = 15%). Currency is USD raw (e.g. 2_000_000_000 for $2B).
Sort keys must be one of the allowed filter keys above.

Return JSON with this shape:
{{
  "filters": {{ "<key>": {{ "min": <number|null>, "max": <number|null> }}, ... }},
  "universe": {{
    "jurisdiction": "US"|"JP"|"INTL",
    "region": "Europe"|"Asia"|"Africa"|"North America"|"South America"|"MENA" | null,
    "country_code": "DE"|"GB"|"KR"|... | null,
    "exchanges": ["NYSE","NASDAQ"] | null,
    "sectors": ["45"] | null,
    "industries": null,
    "portfolio_tickers": null
  }},
  "sort": {{ "key": "<allowed-key>", "dir": "asc"|"desc" }},
  "rationale": "one-sentence explanation in plain English",
  "warnings": ["any concepts you could not map cleanly"]
}}

Do not invent ticker lists. Do not output any keys not in the schema. If a concept like
"small cap" is mentioned, map it to market_cap_usd with a numeric max
(small ≈ 2e9, mid ≈ 1e10, large ≈ 5e10).

Jurisdiction guidance:
- "US" — SEC-registered US companies (NYSE, NASDAQ, US ADRs). country_code should be null.
- "JP" — EDINET-registered Japanese companies (TSE, Prime, Standard, Growth). country_code should be null.
- "INTL" — every non-US, non-Japan universe (Europe, UK, Canada, Korea, Singapore,
  India, South Africa, Hong Kong, China, Latin America, etc.), sourced from Yahoo.
  Pick "INTL" if the query references any non-US, non-Japan region, country,
  currency, or exchange. Geography for INTL is set two ways:
  * If the query names a specific country (e.g. "German industrials", "UK banks",
    "Korean chipmakers"), set country_code to that country's ISO-2 code (DE, GB,
    KR, etc.) and leave region null.
  * If the query names a whole region rather than one country (e.g. "European
    firms", "Asian tech", "African miners"), set region to exactly one of
    "Europe", "Asia", "Africa", "North America", "South America", "MENA" and leave
    country_code null.
  Leave both null only when the query is genuinely global. INTL companies now
  carry GICS sector/industry codes (mapped from Yahoo), so sector/industry
  filters DO apply for INTL — though a minority of names (ETFs, thinly-covered
  tickers) remain unclassified and drop out when a sector/industry is set.
Return ONLY the JSON object."""


def _coerce_ai_payload(data: dict, default_juris: str, portfolio_tickers: list[str] | None) -> ScreenerAiResponse:
    """Pull the LLM output into a clean ScreenerAiResponse, dropping garbage."""
    warnings: list[str] = []
    raw_filters = data.get("filters") or {}
    if not isinstance(raw_filters, dict):
        warnings.append("filters field was not an object; ignored")
        raw_filters = {}

    filters: dict[str, Range] = {}
    for k, v in raw_filters.items():
        if k not in _CAT_BY_KEY:
            warnings.append(f"unknown filter key dropped: {k}")
            continue
        if not isinstance(v, dict):
            warnings.append(f"filter {k} not a range object; ignored")
            continue
        try:
            mn = v.get("min")
            mx = v.get("max")
            mn = float(mn) if mn is not None else None
            mx = float(mx) if mx is not None else None
        except (TypeError, ValueError):
            warnings.append(f"filter {k} had non-numeric bounds; ignored")
            continue
        if mn is None and mx is None:
            continue
        filters[k] = Range(min=mn, max=mx)

    uni_raw = data.get("universe") or {}
    juris = uni_raw.get("jurisdiction") or default_juris
    if juris not in ("US", "JP", "INTL"):
        warnings.append(f"unknown jurisdiction '{juris}' replaced with {default_juris}")
        juris = default_juris

    def _str_list(v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, list):
            return [str(x) for x in v if isinstance(x, (str, int))]
        return None

    country_code: str | None = None
    cc_raw = uni_raw.get("country_code")
    if juris == "INTL" and isinstance(cc_raw, str) and cc_raw.strip():
        candidate = cc_raw.strip().upper()
        # ISO-2 must be exactly two letters; otherwise drop with a warning.
        if len(candidate) == 2 and candidate.isalpha():
            country_code = candidate
        else:
            warnings.append(f"country_code '{cc_raw}' is not a valid ISO-2 code; ignored")

    region: str | None = None
    region_raw = uni_raw.get("region")
    if juris == "INTL" and isinstance(region_raw, str) and region_raw.strip():
        canon = _canonical_region(region_raw)
        if canon:
            region = canon
        else:
            warnings.append(f"region '{region_raw}' is not a recognized INTL region; ignored")

    universe = Universe(
        jurisdiction=juris,
        country_code=country_code,
        region=region,
        exchanges=_str_list(uni_raw.get("exchanges")),
        sectors=_str_list(uni_raw.get("sectors")),
        industries=_str_list(uni_raw.get("industries")),
        portfolio_tickers=portfolio_tickers or _str_list(uni_raw.get("portfolio_tickers")),
    )

    sort_raw = data.get("sort") or {}
    sort_key = sort_raw.get("key") if isinstance(sort_raw, dict) else None
    if sort_key not in _CAT_BY_KEY:
        sort_key = "market_cap_usd"
    sort_dir = sort_raw.get("dir") if isinstance(sort_raw, dict) else None
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    rationale = data.get("rationale") if isinstance(data.get("rationale"), str) else None
    extra_warnings = data.get("warnings") or []
    if isinstance(extra_warnings, list):
        warnings.extend([str(w) for w in extra_warnings if isinstance(w, str)])

    return ScreenerAiResponse(
        filters=filters,
        universe=universe,
        sort=Sort(key=sort_key, dir=sort_dir),
        rationale=rationale,
        warnings=warnings,
    )


@router.post("/ai", response_model=ScreenerAiResponse)
async def screener_ai(req: ScreenerAiRequest) -> ScreenerAiResponse:
    try:
        prov = llm_providers.get(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    api_key = (req.api_key or "").strip() or llm_runtime.resolve_env_key(prov.id)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"No {prov.label} API key configured. Set {prov.env[0]} or pass api_key.",
        )
    model = llm_providers.chat_model(prov.id, req.model)

    try:
        data = await llm_runtime.chat_json(
            api_key=api_key,
            provider=prov.id,
            base_url=req.base_url,
            model=model,
            system_prompt=_ai_system_prompt(),
            user_prompt=req.prompt.strip(),
            temperature=0.1,
            max_tokens=900,
        )
    except llm_runtime.LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _coerce_ai_payload(data, req.jurisdiction, req.portfolio_tickers)


# ---------------------------------------------------------------------------
# /prompt-preview - landing tile -> filters, rows, short DeepSeek summary
# ---------------------------------------------------------------------------

def _preview_summary_system_prompt() -> str:
    return """You write terse equity screener preview notes for a terminal UI.

Use only the supplied rows and filters. Mention the clicked screener tile by name.
Return two short sentences maximum. Do not invent tickers, valuations, or claims.
If rows are empty, say no matching names were found."""


@router.post("/prompt-preview", response_model=ScreenerPromptPreviewResponse)
async def screener_prompt_preview(req: ScreenerPromptPreviewRequest) -> ScreenerPromptPreviewResponse:
    warnings: list[str] = []
    try:
        prov = llm_providers.get(req.provider)
    except llm_providers.LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    api_key = (req.api_key or "").strip() or llm_runtime.resolve_env_key(prov.id)
    if not api_key:
        return ScreenerPromptPreviewResponse(
            label=req.label,
            prompt=req.prompt,
            summary=None,
            rows=[],
            ai_filters=None,
            warnings=[f"{prov.label} API key is not configured; prompt preview is unavailable."],
        )

    base_url = req.base_url
    model = llm_providers.chat_model(prov.id, req.model)

    try:
        data = await llm_runtime.chat_json(
            api_key=api_key,
            provider=prov.id,
            base_url=base_url,
            model=model,
            system_prompt=_ai_system_prompt(),
            user_prompt=req.prompt.strip(),
            temperature=0.1,
            max_tokens=900,
        )
        ai_filters = _coerce_ai_payload(data, req.jurisdiction, None)
    except llm_runtime.LLMError as exc:
        return ScreenerPromptPreviewResponse(
            label=req.label,
            prompt=req.prompt,
            summary=None,
            rows=[],
            ai_filters=None,
            warnings=[f"{prov.label} could not translate the prompt: {exc}"],
        )

    run_resp = await screener_run(
        ScreenerRunRequest(
            universe=ai_filters.universe,
            filters=ai_filters.filters,
            sort=ai_filters.sort,
            limit=8,
        )
    )
    warnings.extend(ai_filters.warnings)

    rows_doc = [
        {
            "ticker": r.ticker,
            "name": r.name,
            "sector": r.sector,
            "metrics": r.metrics,
        }
        for r in run_resp.rows[:6]
    ]
    user_doc = json.dumps(
        {
            "tile": req.label,
            "prompt": req.prompt,
            "filters": {k: v.model_dump() for k, v in ai_filters.filters.items()},
            "sort": ai_filters.sort.model_dump(),
            "rows": rows_doc,
        },
        default=str,
    )

    summary: str | None = None
    try:
        msg = await llm_runtime.chat_once(
            api_key=api_key,
            provider=prov.id,
            base_url=base_url,
            model=model,
            messages=[
                {"role": "system", "content": _preview_summary_system_prompt()},
                {"role": "user", "content": user_doc},
            ],
            temperature=0.2,
            max_tokens=220,
        )
        text = (msg.get("content") or "").strip()
        summary = text or None
    except llm_runtime.LLMError as exc:
        warnings.append(f"DeepSeek could not summarize the preview: {exc}")

    return ScreenerPromptPreviewResponse(
        label=req.label,
        prompt=req.prompt,
        summary=summary,
        rows=run_resp.rows,
        ai_filters=ai_filters,
        warnings=warnings,
    )
