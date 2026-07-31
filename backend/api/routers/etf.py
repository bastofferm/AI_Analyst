"""ETF endpoints for the consumer MVP (DOC WA0007 §6 + §7).

  POST /api/etf/screen            structured screener
  GET  /api/etf/{isin}             detail metrics + 1Y price series
  GET  /api/etf/{isin}/story       AI Story (4 paragraphs, lang en|de)

Reads sec.dim_etf, sec.dim_etf_listing, sec.fact_prices_etf (migration 117).
Reuses the existing macro regime label and the DeepSeek runtime in api/ai.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import llm_providers

from ..ai import llm_runtime
from ..db import acquire


router = APIRouter()
logger = logging.getLogger("mzqa.etf")

Lang = Literal["en", "de"]

# In-process cache for AI stories. Key: (isin, regime_label, lang). Stories are
# stable per (isin, regime_date) per WA0007 §7 — when the regime label changes,
# the cache key changes and we regenerate.
_STORY_CACHE: dict[tuple[str, str, str], "EtfStory"] = {}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EtfScreenFilters(BaseModel):
    asset_class: list[str] | None = None        # Equity, Fixed Income, Commodity, Mixed
    countries: list[str] | None = None          # ["DE", "AT"]
    mics: list[str] | None = None               # operating MICs
    sectors: list[str] | None = None            # portfolio sectors with meaningful weight
    issuers: list[str] | None = None             # provider / issuer names
    provider_ids: list[str] | None = None        # canonical provider IDs from /providers
    ter_max: float | None = None                # 0.005 = 0.5%
    aum_min_eur: float | None = None            # 1_000_000_000 = €1bn
    sfdr_articles: list[str] | None = None      # ["8", "9"]
    replication: list[str] | None = None        # Physical, Synthetic, Sampling
    regime_score_min: int | None = Field(default=None, ge=0, le=10)
    q: str | None = None                        # free-text name/ISIN search


class EtfScreenRequest(BaseModel):
    filters: EtfScreenFilters = Field(default_factory=EtfScreenFilters)
    sort: Literal["regime_score", "ter", "aum", "return_1y", "name"] = "regime_score"
    sort_dir: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class EtfRow(BaseModel):
    isin: str
    name: str                                   # cleaned display name
    issuer_name: str | None
    fund_family: str | None
    index_tracked: str | None
    ter_pct: float | None
    aum_eur: float | None
    asset_class: str | None
    sfdr_article: str | None
    replication_method: str | None
    fund_currency: str | None
    primary_mic: str | None
    primary_country: str | None
    listing_countries: list[str]
    return_1y: float | None
    regime_score: int | None


class EtfScreenResponse(BaseModel):
    total: int
    rows: list[EtfRow]


class EtfProviderFacet(BaseModel):
    provider_id: str
    label: str
    domain: str | None
    aliases: list[str]
    source_status: str
    etf_count: int
    active_etfs: int
    holdings_isins: int
    holdings_coverage: float
    official_success: int = 0
    unsupported: int = 0
    failed: int = 0


class EtfPricePoint(BaseModel):
    date: str
    close: float


class EtfRiskMetrics(BaseModel):
    return_1m: float | None
    return_6m: float | None
    return_1y: float | None
    return_5y_annual: float | None      # CAGR over last 5Y
    return_max_annual: float | None     # CAGR since first price in our DB
    volatility_annual: float | None     # std of daily log-returns × √252
    max_drawdown: float | None          # negative decimal e.g. -0.34
    inception_in_db: str | None         # ISO date of earliest close we have


class EtfRegimePerformance(BaseModel):
    regime: str                         # 'Early-expansion' | 'Mid-expansion' | 'Late-cycle' | 'Contraction'
    days_observed: int
    annualized_return: float | None
    last_occurrence: str | None         # ISO date of most recent day in this regime


class EtfHolding(BaseModel):
    rank: int
    symbol: str | None
    holding_isin: str | None = None
    name: str | None
    weight: float | None
    cik: str | None = None
    edinet_code: str | None = None
    logo_url: str | None = None
    resolved_company_id: str | None = None
    resolution_source: str | None = None


class EtfSectorWeight(BaseModel):
    sector: str
    weight: float | None


class EtfIndustryWeight(BaseModel):
    industry: str
    weight: float | None


class EtfCreditQualityWeight(BaseModel):
    rating: str
    weight: float | None


class EtfFactorLoading(BaseModel):
    model: str                          # FF5 | FF6
    ff_region: str | None
    n_obs: int | None
    window_start: str | None
    window_end: str | None
    alpha: float | None                 # annualized
    beta_mkt: float | None
    beta_smb: float | None
    beta_hml: float | None
    beta_mom: float | None
    beta_rmw: float | None
    beta_cma: float | None
    t_mkt: float | None
    t_smb: float | None
    t_hml: float | None
    t_mom: float | None
    t_rmw: float | None
    t_cma: float | None
    r2: float | None
    adj_r2: float | None


class EtfFactorPerformancePoint(BaseModel):
    date: str
    close: float


class EtfFactorPerformance(BaseModel):
    factor: str                         # MKT | SMB | HML | RMW | CMA | MOM
    factor_key: str                     # fact_fama_french factor key
    label: str
    total_return: float | None
    volatility_annual: float | None
    var_95: float | None                # backward-compatible: positive daily 95% historical VaR
    var_95_annual_weekly: float | None  # positive annualized 95% historical VaR from weekly returns
    max_drawdown: float | None
    max_drawdown_days: int | None
    observations: int
    points: list[EtfFactorPerformancePoint]


class EtfProfile(BaseModel):
    clean_name: str | None
    fund_family: str | None
    category: str | None
    stock_pct: float | None
    bond_pct: float | None
    cash_pct: float | None
    other_pct: float | None
    pe_ratio: float | None
    pb_ratio: float | None
    holdings: list[EtfHolding]
    sectors: list[EtfSectorWeight]
    industries: list[EtfIndustryWeight] = Field(default_factory=list)
    credit_quality: list[EtfCreditQualityWeight] = Field(default_factory=list)


class EtfDetail(BaseModel):
    isin: str
    full_name: str
    display_name: str                   # cleaned name (profile.clean_name preferred)
    short_name: str | None
    issuer_name: str | None
    fund_family: str | None
    index_tracked: str | None
    asset_class: str | None
    replication_method: str | None
    ter_pct: float | None
    aum_eur: float | None
    sfdr_article: str | None
    fund_currency: str | None
    inception_date: str | None
    is_active: bool
    price_series: list[EtfPricePoint]   # daily closes for the requested period
    period: str                         # '1m' | '6m' | '1y' | '5y' | 'max' (echoed back)
    return_1y: float | None
    regime_score: int | None
    risk_metrics: EtfRiskMetrics | None
    regime_performance: list[EtfRegimePerformance]
    profile: EtfProfile | None
    factors: list[EtfFactorLoading]
    factor_performance: list[EtfFactorPerformance]


class EtfStory(BaseModel):
    isin: str
    regime: str | None
    regime_date: str | None
    regime_score: int | None
    story_en: str | None
    story_de: str | None
    generated_at: str
    disclaimer: str
    model: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _current_regime() -> tuple[str | None, str | None]:
    """Best-effort current regime label + date.

    Primary source: fact_macro_cycle_assessment.phase (the synthesized cycle
    assessment the consumer dashboard renders). Falling back to
    fact_macro_factor.regime_label if the cycle table is empty, and finally to
    'Late Cycle' so the AI prompt always has something to anchor to.

    Both sources are mapped via _CANONICAL_REGIME for scoring.
    """
    try:
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT phase, period_end
                FROM fact_macro_cycle_assessment
                WHERE jurisdiction = 'US'
                ORDER BY period_end DESC LIMIT 1
                """
            )
            if row and row["phase"]:
                phase = row["phase"].replace("_", " ").title()  # late_cycle -> Late Cycle
                return phase, row["period_end"].isoformat() if row["period_end"] else None
            # Fallback: legacy factor-based regime label
            row = await conn.fetchrow(
                """
                SELECT regime_label, date
                FROM fact_macro_factor
                WHERE factor_id = $1
                ORDER BY date DESC LIMIT 1
                """,
                "us_cycle",
            )
            if row and row["regime_label"]:
                return row["regime_label"], row["date"].isoformat() if row["date"] else None
    except Exception as exc:  # noqa: BLE001
        logger.info("etf.regime fallback: %s", exc)
    return "Late Cycle", date.today().isoformat()


# Heuristic regime fit (WA0007 §6.3). Weighted: asset class (50%) + replication
# (10%) + TER tier (15%) + AUM tier (25%). Returns 0-10. The full factor model
# arrives in M5; this gives a sane preview today.
#
# Keys MUST match the regime labels emitted by the macro pipeline:
#   - fact_macro_factor.regime_label uses Title-Case "Early-expansion" etc.
#   - fact_macro_cycle_assessment.phase uses lowercase "late_cycle" etc.
# _CANONICAL_REGIME maps both spellings (plus historical synonyms) to one of the
# four canonical buckets used by _REGIME_CLASS_PREFS.
_REGIME_CLASS_PREFS: dict[str, dict[str, int]] = {
    "early_expansion": {"Equity": 9, "Fixed Income": 5, "Commodity": 7, "Mixed": 7},
    "mid_expansion":   {"Equity": 8, "Fixed Income": 6, "Commodity": 6, "Mixed": 7},
    "late_cycle":      {"Equity": 6, "Fixed Income": 8, "Commodity": 7, "Mixed": 6},
    "contraction":     {"Equity": 3, "Fixed Income": 9, "Commodity": 5, "Mixed": 5},
}

_CANONICAL_REGIME: dict[str, str] = {
    # Title-Case (fact_macro_factor.regime_label)
    "early-expansion": "early_expansion",
    "mid-expansion": "mid_expansion",
    "late-cycle": "late_cycle",
    "contraction": "contraction",
    # Lowercase (fact_macro_cycle_assessment.phase)
    "expansion": "mid_expansion",
    "recovery": "early_expansion",
    "slowdown": "late_cycle",
    "recession": "contraction",
    "mixed": "late_cycle",
    # Historical synonyms preserved for older clients / dashboards
    "early cycle": "early_expansion",
    "mid cycle": "mid_expansion",
    "late cycle": "late_cycle",
}


def _canon_regime(regime: str | None) -> str:
    return _CANONICAL_REGIME.get((regime or "").strip().lower(), "late_cycle")


def _regime_score(regime: str | None, asset_class: str | None,
                  ter_pct: float | None, aum_eur: float | None) -> int:
    cls_pref = _REGIME_CLASS_PREFS[_canon_regime(regime)]
    cls_score = cls_pref.get(asset_class or "Equity", 5)
    ter_score = 8 if (ter_pct is not None and ter_pct <= 0.0025) else 6 if (ter_pct is not None and ter_pct <= 0.005) else 4
    aum_score = 9 if (aum_eur and aum_eur >= 1e9) else 7 if (aum_eur and aum_eur >= 1e8) else 5
    raw = 0.50 * cls_score + 0.15 * ter_score + 0.25 * aum_score + 0.10 * 6
    return max(0, min(10, round(raw)))


import re as _re

_NAME_NOISE = [
    (_re.compile(r"\bRegistered Shares\b", _re.I), ""),
    (_re.compile(r"\bReg\.?\s*Shares\b", _re.I), ""),
    (_re.compile(r"\bRegistered\b", _re.I), ""),
    (_re.compile(r"\bInhaber-?Anteile\b", _re.I), ""),
    (_re.compile(r"\bo\.?\s*N\.?\b", _re.I), ""),
    (_re.compile(r"\boN\b"), ""),
    (_re.compile(r"\bDis\.?oN\b", _re.I), "(Dist)"),
    (_re.compile(r"\bU\.ETF\b", _re.I), "UCITS ETF"),
    (_re.compile(r"\bU\.?\s?ETF\b", _re.I), "UCITS ETF"),
]
_ISSUER_EXPAND = [
    (_re.compile(r"^iSh?s(?:I{1,3}|IV|V|VI|VII)?\b", _re.I), "iShares"),
    (_re.compile(r"^Xtr\b", _re.I), "Xtrackers"),
    (_re.compile(r"^Amundi IS\b", _re.I), "Amundi"),
]


def _clean_name(raw: str | None) -> str:
    """Strip FIRDS share-class boilerplate. Used as a fallback when the
    yfinance clean_name is absent."""
    if not raw:
        return "—"
    s = raw
    for rx, rep in _ISSUER_EXPAND:
        s = rx.sub(rep, s)
    for rx, rep in _NAME_NOISE:
        s = rx.sub(rep, s)
    s = _re.sub(r"\s{2,}", " ", s)
    s = _re.sub(r"\s+([,)])", r"\1", s)
    s = _re.sub(r"\(\s*\)", "", s)
    s = _re.sub(r"[-–·,\s]+$", "", s).strip()
    return s or raw


def _classify_asset_class(name: str | None, index_tracked: str | None) -> str:
    """Infer asset class from name/index until Xetra enrichment fills the column."""
    text = " ".join(filter(None, (name, index_tracked))).lower()
    if any(k in text for k in (" bond", " bonds", " bd ", "treasury", "govt", "aggregate", "credit", "high yield", "ig ", " hy ")):
        return "Fixed Income"
    if any(k in text for k in ("gold", "silver", "commodity", "commodities", "metals", "oil", "energy etc")):
        return "Commodity"
    if "multi-asset" in text or " mixed " in text or "balanced" in text:
        return "Mixed"
    return "Equity"


def _return_1y(prices: list[tuple[date, float]]) -> float | None:
    if len(prices) < 2:
        return None
    first = prices[0][1]
    last = prices[-1][1]
    if not first or first <= 0:
        return None
    return (last - first) / first


# ---------------------------------------------------------------------------
# Risk metrics + regime-performance computed from fact_prices_etf
# ---------------------------------------------------------------------------

_PERIOD_DAYS: dict[str, int | None] = {
    "1m": 31, "6m": 186, "1y": 400, "5y": 1830, "max": None,
}


def _compute_risk_metrics(all_prices: list[tuple[date, float]]) -> EtfRiskMetrics | None:
    """All inputs are full-history (close, date) ascending. Returns None if
    we lack at least 30 daily closes — the cheapest signal of "tradable
    history" without false-positive on first-week new ETFs."""
    if len(all_prices) < 30:
        return None
    import math

    closes = [c for _, c in all_prices]
    dates = [d for d, _ in all_prices]
    last = closes[-1]

    def _ret_back(days: int) -> float | None:
        cutoff = dates[-1] - timedelta(days=days)
        # walk backwards to find the first close on/before cutoff
        for i in range(len(closes) - 1, -1, -1):
            if dates[i] <= cutoff:
                first = closes[i]
                if first and first > 0:
                    return (last - first) / first
                break
        return None

    def _cagr_back(days: int) -> float | None:
        cutoff = dates[-1] - timedelta(days=days)
        for i in range(len(closes) - 1, -1, -1):
            if dates[i] <= cutoff:
                first = closes[i]
                actual_days = (dates[-1] - dates[i]).days
                if first and first > 0 and actual_days > 0:
                    years = actual_days / 365.25
                    return (last / first) ** (1.0 / years) - 1.0
                break
        return None

    # CAGR since inception (using the first close in our DB)
    first_d, first_c = all_prices[0]
    years_total = (dates[-1] - first_d).days / 365.25
    cagr_max = (last / first_c) ** (1.0 / years_total) - 1.0 if first_c > 0 and years_total > 0.25 else None

    # Daily log-returns -> annualised volatility
    log_rets: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            log_rets.append(math.log(cur / prev))
    if len(log_rets) >= 30:
        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
        vol_annual = math.sqrt(var) * math.sqrt(252)
    else:
        vol_annual = None

    # Max drawdown
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    return EtfRiskMetrics(
        return_1m=_ret_back(30),
        return_6m=_ret_back(182),
        return_1y=_ret_back(365),
        return_5y_annual=_cagr_back(5 * 365),
        return_max_annual=cagr_max,
        volatility_annual=vol_annual,
        max_drawdown=max_dd if max_dd < 0 else None,
        inception_in_db=first_d.isoformat(),
    )


_FF_PERFORMANCE_FACTORS: list[tuple[str, str, str, str]] = [
    ("MKT", "Mkt-RF", "Market", "market beta"),
    ("SMB", "SMB", "Size", "small minus big"),
    ("HML", "HML", "Value", "value minus growth"),
    ("RMW", "RMW", "Profitability", "robust minus weak"),
    ("CMA", "CMA", "Investment", "conservative minus aggressive"),
    ("MOM", "Mom", "Momentum", "recent winners minus losers"),
]


def _ff_performance_dataset(region: str | None, model: str | None, factor_key: str) -> str:
    region_key = (region or "US").upper()
    model_key = (model or "FF6").upper()
    developed = region_key in {"DEV", "DEVELOPED", "EU", "EUR", "EZ", "EUROPE", "GLOBAL", "WORLD"}
    if factor_key == "Mom":
        return "Developed_Mom_Factor_Daily" if developed else "F-F_Momentum_Factor_daily"
    if region_key == "JP":
        return "Japan_5_Factors_Daily" if model_key in {"FF5", "FF6"} else "Japan_3_Factors_Daily"
    if developed:
        return "Developed_5_Factors_Daily"
    return "F-F_Research_Data_5_Factors_2x3_daily" if model_key in {"FF5", "FF6"} else "F-F_Research_Data_Factors_daily"


def _sample_factor_points(points: list[EtfFactorPerformancePoint], max_points: int = 96) -> list[EtfFactorPerformancePoint]:
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = [point for index, point in enumerate(points) if index % step == 0]
    if sampled[-1].date != points[-1].date:
        sampled.append(points[-1])
    return sampled


def _factor_metric_series(
    rows: list[tuple[date, float]],
    factor: str,
    factor_key: str,
    label: str,
) -> EtfFactorPerformance | None:
    if len(rows) < 20:
        return None
    import math

    level = 100.0
    points: list[EtfFactorPerformancePoint] = []
    returns: list[float] = []
    weekly_returns: list[float] = []
    current_week: tuple[int, int] | None = None
    current_week_return = 1.0
    peak = 100.0
    peak_date = rows[0][0]
    max_dd = 0.0
    max_dd_days = 0
    for d, percent_return in rows:
        daily_return = percent_return / 100.0
        if daily_return <= -0.99:
            continue
        week_key = d.isocalendar()[:2]
        if current_week is None:
            current_week = week_key
        elif week_key != current_week:
            weekly_returns.append(current_week_return - 1.0)
            current_week = week_key
            current_week_return = 1.0
        current_week_return *= 1.0 + daily_return
        level *= 1.0 + daily_return
        returns.append(daily_return)
        if level > peak:
            peak = level
            peak_date = d
        drawdown = (level - peak) / peak if peak > 0 else 0.0
        if drawdown < max_dd:
            max_dd = drawdown
            max_dd_days = max(0, (d - peak_date).days)
        points.append(EtfFactorPerformancePoint(date=d.isoformat(), close=round(level, 4)))
    if current_week is not None:
        weekly_returns.append(current_week_return - 1.0)

    if len(points) < 20 or not returns:
        return None
    total_return = points[-1].close / 100.0 - 1.0
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        volatility = math.sqrt(variance) * math.sqrt(252)
    else:
        volatility = None
    sorted_returns = sorted(returns)
    var_index = max(0, min(len(sorted_returns) - 1, int((len(sorted_returns) - 1) * 0.05)))
    var_95 = max(0.0, -sorted_returns[var_index])
    if len(weekly_returns) >= 4:
        sorted_weekly = sorted(weekly_returns)
        weekly_var_index = max(0, min(len(sorted_weekly) - 1, int((len(sorted_weekly) - 1) * 0.05)))
        var_95_annual_weekly = max(0.0, -sorted_weekly[weekly_var_index]) * math.sqrt(52)
    else:
        var_95_annual_weekly = None

    return EtfFactorPerformance(
        factor=factor,
        factor_key=factor_key,
        label=label,
        total_return=total_return,
        volatility_annual=volatility,
        var_95=var_95,
        var_95_annual_weekly=var_95_annual_weekly,
        max_drawdown=max_dd if max_dd < 0 else None,
        max_drawdown_days=max_dd_days if max_dd < 0 else None,
        observations=len(returns),
        points=_sample_factor_points(points),
    )


async def _fetch_factor_performance(
    conn,
    region: str | None,
    model: str | None,
    days: int = 400,
) -> list[EtfFactorPerformance]:
    factor_keys = [factor_key for _, factor_key, _, _ in _FF_PERFORMANCE_FACTORS]
    datasets = [_ff_performance_dataset(region, model, factor_key) for factor_key in factor_keys]
    latest = await conn.fetchval(
        """
        SELECT MAX(ff.date)
        FROM fact_fama_french ff
        JOIN UNNEST($1::text[], $2::text[]) AS pair(factor, dataset)
          ON ff.factor = pair.factor AND ff.dataset = pair.dataset
        WHERE ff.value IS NOT NULL
        """,
        factor_keys,
        datasets,
    )
    if latest is None:
        return []
    start = latest - timedelta(days=days)
    rows = await conn.fetch(
        """
        SELECT ff.date, ff.factor, ff.value
        FROM fact_fama_french ff
        JOIN UNNEST($1::text[], $2::text[]) AS pair(factor, dataset)
          ON ff.factor = pair.factor AND ff.dataset = pair.dataset
        WHERE ff.date BETWEEN $3 AND $4
          AND ff.value IS NOT NULL
        ORDER BY ff.factor, ff.date
        """,
        factor_keys,
        datasets,
        start,
        latest,
    )
    by_factor: dict[str, list[tuple[date, float]]] = {factor_key: [] for factor_key in factor_keys}
    for row in rows:
        by_factor.setdefault(row["factor"], []).append((row["date"], float(row["value"])))

    out: list[EtfFactorPerformance] = []
    for factor, factor_key, label, _description in _FF_PERFORMANCE_FACTORS:
        metric = _factor_metric_series(by_factor.get(factor_key, []), factor, factor_key, label)
        if metric is not None:
            out.append(metric)
    return out


async def _fetch_daily_regime_map(conn) -> dict[date, str]:
    """Build a date -> canonical regime label map from fact_macro_factor /
    fact_macro_cycle_assessment. We forward-fill so every trading day inherits
    the most recent regime read; the macro pipeline emits ~monthly so this is
    necessary for the join to be useful."""
    try:
        rows = await conn.fetch(
            """
            SELECT date AS d, regime_label AS label
            FROM fact_macro_factor
            WHERE factor_id = 'us_cycle' AND regime_label IS NOT NULL
            ORDER BY date
            """
        )
    except Exception:
        rows = []
    if not rows:
        # Fallback: cycle assessment (only emits a handful of phase changes)
        try:
            rows = await conn.fetch(
                """
                SELECT period_end AS d, phase AS label
                FROM fact_macro_cycle_assessment
                WHERE jurisdiction = 'US'
                ORDER BY period_end
                """
            )
        except Exception:
            rows = []
    def _norm(s: str) -> str:
        # Normalize to canonical Title-case-with-lower-suffix: "Early-expansion",
        # "Late-cycle", "Contraction". Matches _CANONICAL_REGIME keys + the order
        # map below.
        s = s.strip().replace("_", "-")
        parts = s.split("-")
        if len(parts) == 2:
            return f"{parts[0].capitalize()}-{parts[1].lower()}"
        return s.capitalize()
    return {r["d"]: _norm(r["label"]) for r in rows if r["label"]}


def _regime_performance(
    all_prices: list[tuple[date, float]],
    regime_map: dict[date, str],
) -> list[EtfRegimePerformance]:
    """Tag each daily return with the most recent regime label observed on or
    before that day, then compound returns per regime to get a regime-bucketed
    annualised return."""
    if len(all_prices) < 30 or not regime_map:
        return []
    import math

    sorted_regime_dates = sorted(regime_map.keys())

    def _regime_for(d: date) -> str | None:
        # binary-search the most recent regime read on/before d
        lo, hi = 0, len(sorted_regime_dates) - 1
        best = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if sorted_regime_dates[mid] <= d:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return regime_map[sorted_regime_dates[best]] if best >= 0 else None

    # Bucketed sums of daily log-returns + day counts + last day
    bucket_log: dict[str, float] = {}
    bucket_days: dict[str, int] = {}
    bucket_last: dict[str, date] = {}
    for (prev_d, prev_c), (cur_d, cur_c) in zip(all_prices, all_prices[1:]):
        if prev_c <= 0 or cur_c <= 0:
            continue
        regime = _regime_for(cur_d)
        if not regime:
            continue
        bucket_log[regime] = bucket_log.get(regime, 0.0) + math.log(cur_c / prev_c)
        bucket_days[regime] = bucket_days.get(regime, 0) + 1
        bucket_last[regime] = cur_d

    out: list[EtfRegimePerformance] = []
    for regime, total_log in bucket_log.items():
        days = bucket_days[regime]
        if days < 5:
            continue
        avg_daily = total_log / days
        annualised = math.exp(avg_daily * 252) - 1.0
        out.append(EtfRegimePerformance(
            regime=regime,
            days_observed=days,
            annualized_return=annualised,
            last_occurrence=bucket_last[regime].isoformat(),
        ))
    # Canonical order
    order = {"Early-expansion": 0, "Mid-expansion": 1, "Late-cycle": 2, "Contraction": 3}
    out.sort(key=lambda r: order.get(r.regime, 99))
    return out


# ---------------------------------------------------------------------------
# /providers
# ---------------------------------------------------------------------------

@router.get("/providers", response_model=list[EtfProviderFacet])
async def providers() -> list[EtfProviderFacet]:
    async with acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT pv.provider_id, pv.label, pv.domain, pv.aliases, pv.source_status,
                       COUNT(DISTINCT d.isin) AS etf_count,
                       COUNT(DISTINCT d.isin) FILTER (WHERE COALESCE(d.is_active, TRUE)) AS active_etfs,
                       COUNT(DISTINCT h.isin) AS holdings_isins,
                       COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'success') AS official_success,
                       COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'unsupported') AS unsupported,
                       COUNT(DISTINCT fs.isin) FILTER (WHERE fs.status = 'failed') AS failed
                FROM sec.dim_etf_provider pv
                LEFT JOIN sec.dim_etf d ON d.provider_id = pv.provider_id
                LEFT JOIN sec.etf_holding h ON h.isin = d.isin
                LEFT JOIN sec.etf_holdings_fetch_state fs ON fs.isin = d.isin
                GROUP BY pv.provider_id, pv.label, pv.domain, pv.aliases, pv.source_status
                HAVING COUNT(DISTINCT d.isin) > 0 OR pv.provider_id = 'unknown_provider'
                ORDER BY COUNT(DISTINCT d.isin) DESC, pv.label
                """
            )
        except Exception as exc:  # noqa: BLE001 - schema may be unapplied in dev
            logger.warning("etf.providers registry unavailable: %s", exc)
            rows = await conn.fetch(
                """
                SELECT lower(regexp_replace(COALESCE(NULLIF(btrim(d.issuer_name), ''), 'unknown_provider'), '[^a-zA-Z0-9]+', '_', 'g')) AS provider_id,
                       COALESCE(NULLIF(btrim(d.issuer_name), ''), 'Unknown Provider') AS label,
                       NULL::text AS domain,
                       ARRAY[]::text[] AS aliases,
                       'fallback_only'::text AS source_status,
                       COUNT(DISTINCT d.isin) AS etf_count,
                       COUNT(DISTINCT d.isin) FILTER (WHERE COALESCE(d.is_active, TRUE)) AS active_etfs,
                       COUNT(DISTINCT h.isin) AS holdings_isins,
                       0::int AS official_success,
                       0::int AS unsupported,
                       0::int AS failed
                FROM sec.dim_etf d
                LEFT JOIN sec.etf_holding h ON h.isin = d.isin
                GROUP BY 1,2
                ORDER BY COUNT(DISTINCT d.isin) DESC, label
                """
            )

    out: list[EtfProviderFacet] = []
    for row in rows:
        etf_count = int(row["etf_count"] or 0)
        holdings_isins = int(row["holdings_isins"] or 0)
        out.append(EtfProviderFacet(
            provider_id=row["provider_id"],
            label=row["label"],
            domain=row["domain"],
            aliases=list(row["aliases"] or []),
            source_status=row["source_status"],
            etf_count=etf_count,
            active_etfs=int(row["active_etfs"] or 0),
            holdings_isins=holdings_isins,
            holdings_coverage=(holdings_isins / etf_count) if etf_count else 0.0,
            official_success=int(row["official_success"] or 0),
            unsupported=int(row["unsupported"] or 0),
            failed=int(row["failed"] or 0),
        ))
    return out


# ---------------------------------------------------------------------------
# /screen
# ---------------------------------------------------------------------------

@router.post("/screen", response_model=EtfScreenResponse)
async def screen(req: EtfScreenRequest) -> EtfScreenResponse:
    f = req.filters
    where: list[str] = ["COALESCE(d.is_active, TRUE) = TRUE"]
    args: list[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.replace("?", f"${len(args)}"))

    if f.asset_class:
        add("d.asset_class = ANY(?)", f.asset_class)
    if f.provider_ids:
        add("d.provider_id = ANY(?)", f.provider_ids)
    if f.issuers:
        args.append([f"%{issuer}%" for issuer in f.issuers])
        where.append(
            f"(d.issuer_name ILIKE ANY(${len(args)}) "
            f"OR p.fund_family ILIKE ANY(${len(args)}) "
            f"OR pv.label ILIKE ANY(${len(args)}))"
        )
    if f.ter_max is not None:
        add("d.ter_pct <= ?", f.ter_max)
    if f.aum_min_eur is not None:
        add("d.aum_eur >= ?", f.aum_min_eur)
    if f.sfdr_articles:
        add("d.sfdr_article = ANY(?)", f.sfdr_articles)
    if f.sectors:
        args.append(f.sectors)
        where.append(
            "EXISTS (SELECT 1 FROM sec.etf_sector_weight sw "
            f"WHERE sw.isin = d.isin AND sw.sector = ANY(${len(args)}) "
            "AND COALESCE(sw.weight, 0) >= 0.20)"
        )
    if f.replication:
        add("d.replication_method = ANY(?)", f.replication)
    if f.q:
        # ILIKE on name + ISIN — basic NL hook; richer NL parsing is post-MVP.
        like = f"%{f.q.strip()}%"
        args.append(like)
        args.append(like)
        args.append(like)
        where.append(
            f"(d.full_name ILIKE ${len(args)-2} "
            f"OR d.index_tracked ILIKE ${len(args)-1} "
            f"OR d.isin ILIKE ${len(args)})"
        )
    if f.countries or f.mics:
        listing_conds: list[str] = []
        if f.countries:
            args.append(f.countries)
            listing_conds.append(f"l2.country = ANY(${len(args)})")
        if f.mics:
            args.append(f.mics)
            listing_conds.append(f"l2.mic = ANY(${len(args)})")
        where.append(
            "EXISTS (SELECT 1 FROM sec.dim_etf_listing l2 "
            f"WHERE l2.isin = d.isin AND {' AND '.join(listing_conds)})"
        )

    # Cap candidates for in-Python ranking. Hard ceiling 4_000 ETFs total in DE/AT,
    # so this is comfortably the whole universe in worst case.
    sql = f"""
        SELECT d.isin, d.full_name, p.clean_name,
               COALESCE(NULLIF(btrim(d.issuer_name), ''), pv.label) AS issuer_name,
               p.fund_family,
               d.index_tracked, d.ter_pct, d.aum_eur,
               d.asset_class, d.sfdr_article, d.replication_method, d.fund_currency,
               (SELECT l.mic FROM sec.dim_etf_listing l WHERE l.isin = d.isin
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic LIMIT 1) AS primary_mic,
               (SELECT l.country FROM sec.dim_etf_listing l WHERE l.isin = d.isin
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic LIMIT 1) AS primary_country
               , ARRAY(SELECT DISTINCT l.country FROM sec.dim_etf_listing l
                       WHERE l.isin = d.isin AND l.country IS NOT NULL
                       ORDER BY l.country) AS listing_countries
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        LEFT JOIN sec.dim_etf_provider pv ON pv.provider_id = d.provider_id
        WHERE {' AND '.join(where)}
        LIMIT 6000
    """
    async with acquire() as conn:
        records = await conn.fetch(sql, *args)
        # Latest 1Y return via window. Pulled in one round trip after the filter.
        isins = [r["isin"] for r in records]
        returns: dict[str, float] = {}
        if isins:
            ret_rows = await conn.fetch(
                """
                WITH ranked AS (
                  SELECT isin, mic, price_date, close,
                         row_number() OVER (PARTITION BY isin ORDER BY price_date) AS rn_asc,
                         row_number() OVER (PARTITION BY isin ORDER BY price_date DESC) AS rn_desc
                  FROM sec.fact_prices_etf
                  WHERE isin = ANY($1::text[])
                    AND price_date >= (CURRENT_DATE - INTERVAL '400 days')
                )
                SELECT isin,
                       max(CASE WHEN rn_asc=1 THEN close END) AS first_close,
                       max(CASE WHEN rn_desc=1 THEN close END) AS last_close
                FROM ranked GROUP BY isin
                """,
                isins,
            )
            for r in ret_rows:
                first, last = r["first_close"], r["last_close"]
                if first and last and first > 0:
                    returns[r["isin"]] = float((last - first) / first)

    regime, _regime_date = await _current_regime()

    rows: list[EtfRow] = []
    for r in records:
        ac = r["asset_class"] or _classify_asset_class(r["full_name"], r["index_tracked"])
        score = _regime_score(regime, ac, r["ter_pct"], r["aum_eur"])
        display = r["clean_name"] or _clean_name(r["full_name"])
        rows.append(EtfRow(
            isin=r["isin"], name=display,
            issuer_name=r["issuer_name"], fund_family=r["fund_family"],
            index_tracked=r["index_tracked"],
            ter_pct=float(r["ter_pct"]) if r["ter_pct"] is not None else None,
            aum_eur=float(r["aum_eur"]) if r["aum_eur"] is not None else None,
            asset_class=ac, sfdr_article=r["sfdr_article"],
            replication_method=r["replication_method"],
            fund_currency=r["fund_currency"], primary_mic=r["primary_mic"],
            primary_country=r["primary_country"],
            listing_countries=list(r["listing_countries"] or []),
            return_1y=returns.get(r["isin"]), regime_score=score,
        ))

    if f.regime_score_min is not None:
        rows = [row for row in rows if (row.regime_score or 0) >= f.regime_score_min]

    sort_key = {
        "regime_score": lambda r: (r.regime_score or 0),
        "ter": lambda r: (r.ter_pct if r.ter_pct is not None else 1.0),
        "aum": lambda r: (r.aum_eur or 0.0),
        "return_1y": lambda r: (r.return_1y if r.return_1y is not None else -1.0),
        "name": lambda r: (r.name or "").lower(),
    }[req.sort]
    rows.sort(key=sort_key, reverse=(req.sort_dir == "desc"))

    total = len(rows)
    return EtfScreenResponse(total=total, rows=rows[req.offset : req.offset + req.limit])


# ---------------------------------------------------------------------------
# /{isin}
# ---------------------------------------------------------------------------

@router.get("/{isin}", response_model=EtfDetail)
async def detail(
    isin: str,
    period: str = "1y",
) -> EtfDetail:
    # Plain default rather than Query(...) so /story can call detail() directly
    # without picking up the unbound Query sentinel as the period.
    if period not in _PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="period must be one of 1m|6m|1y|5y|max")
    isin = isin.strip().upper()
    if not (12 <= len(isin) <= 12 and isin.isalnum()):
        raise HTTPException(status_code=400, detail="isin must be 12 alphanumeric chars")

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT d.isin, d.full_name, d.short_name,
                   COALESCE(NULLIF(btrim(d.issuer_name), ''), pv.label) AS issuer_name,
                   d.index_tracked,
                   d.asset_class, d.replication_method, d.ter_pct, d.aum_eur,
                   d.sfdr_article, d.fund_currency, d.inception_date, d.is_active,
                   p.clean_name, p.fund_family, p.category,
                   p.stock_pct, p.bond_pct, p.cash_pct, p.other_pct, p.pe_ratio, p.pb_ratio
            FROM sec.dim_etf d
            LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
            LEFT JOIN sec.dim_etf_provider pv ON pv.provider_id = d.provider_id
            WHERE d.isin = $1
            """,
            isin,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="ETF not found")

        holdings_rows = await conn.fetch(
            """
            SELECT rank, symbol, holding_isin, name, weight, cik, edinet_code,
                   logo_url, resolved_company_id, resolution_source
            FROM sec.etf_holding
            WHERE isin=$1 ORDER BY rank
            """,
            isin,
        )
        sector_rows = await conn.fetch(
            "SELECT sector, weight FROM sec.etf_sector_weight WHERE isin=$1 ORDER BY weight DESC",
            isin,
        )
        industry_rows = await conn.fetch(
            "SELECT industry, weight FROM sec.etf_industry_weight WHERE isin=$1 ORDER BY weight DESC",
            isin,
        )
        credit_rows = await conn.fetch(
            "SELECT rating, weight FROM sec.etf_credit_quality_weight WHERE isin=$1 ORDER BY weight DESC",
            isin,
        )
        factor_rows = await conn.fetch(
            """
            SELECT model, ff_region, n_obs, window_start, window_end, alpha,
                   beta_mkt, beta_smb, beta_hml, beta_mom, beta_rmw, beta_cma,
                   t_mkt, t_smb, t_hml, t_mom, t_rmw, t_cma, r2, adj_r2
            FROM sec.fact_etf_factor_loadings WHERE isin=$1 ORDER BY model
            """,
            isin,
        )
        primary_factor_row = next(
            (fr for fr in factor_rows if fr["model"] == "FF6"),
            next((fr for fr in factor_rows if fr["model"] == "FF5"), factor_rows[0] if factor_rows else None),
        )
        factor_performance_rows = await _fetch_factor_performance(
            conn,
            primary_factor_row["ff_region"] if primary_factor_row else "US",
            primary_factor_row["model"] if primary_factor_row else "FF6",
        )

        # Full history for risk metrics + regime performance; the series we ship
        # to the client is filtered to the requested window after.
        all_prices_rows = await conn.fetch(
            """
            SELECT price_date, close FROM sec.fact_prices_etf
            WHERE isin = $1 ORDER BY price_date
            """,
            isin,
        )
        regime_map = await _fetch_daily_regime_map(conn)

    all_prices = [(p["price_date"], float(p["close"])) for p in all_prices_rows]
    cutoff_days = _PERIOD_DAYS[period]
    if cutoff_days is not None and all_prices:
        cutoff = all_prices[-1][0] - timedelta(days=cutoff_days)
        window_prices = [(d, c) for d, c in all_prices if d >= cutoff]
    else:
        window_prices = all_prices

    risk = _compute_risk_metrics(all_prices)
    perf = _regime_performance(all_prices, regime_map)

    regime, _ = await _current_regime()
    ac = row["asset_class"] or _classify_asset_class(row["full_name"], row["index_tracked"])
    score = _regime_score(regime, ac, row["ter_pct"], row["aum_eur"])

    def _f(v):
        return float(v) if v is not None else None

    profile_obj = EtfProfile(
        clean_name=row["clean_name"],
        fund_family=row["fund_family"],
        category=row["category"],
        stock_pct=_f(row["stock_pct"]), bond_pct=_f(row["bond_pct"]),
        cash_pct=_f(row["cash_pct"]), other_pct=_f(row["other_pct"]),
        pe_ratio=_f(row["pe_ratio"]), pb_ratio=_f(row["pb_ratio"]),
        holdings=[EtfHolding(
            rank=h["rank"], symbol=h["symbol"], holding_isin=h["holding_isin"],
            name=h["name"], weight=_f(h["weight"]), cik=h["cik"], edinet_code=h["edinet_code"],
            logo_url=h["logo_url"], resolved_company_id=h["resolved_company_id"],
            resolution_source=h["resolution_source"],
        ) for h in holdings_rows],
        sectors=[EtfSectorWeight(sector=s["sector"], weight=_f(s["weight"])) for s in sector_rows],
        industries=[EtfIndustryWeight(industry=i["industry"], weight=_f(i["weight"])) for i in industry_rows],
        credit_quality=[EtfCreditQualityWeight(rating=c["rating"], weight=_f(c["weight"])) for c in credit_rows],
    )
    # Only attach a profile object if it carries something useful.
    has_profile = bool(row["clean_name"] or holdings_rows or sector_rows or industry_rows or credit_rows or row["stock_pct"] is not None)

    factors = [EtfFactorLoading(
        model=fr["model"], ff_region=fr["ff_region"], n_obs=fr["n_obs"],
        window_start=fr["window_start"].isoformat() if fr["window_start"] else None,
        window_end=fr["window_end"].isoformat() if fr["window_end"] else None,
        alpha=_f(fr["alpha"]),
        beta_mkt=_f(fr["beta_mkt"]), beta_smb=_f(fr["beta_smb"]), beta_hml=_f(fr["beta_hml"]),
        beta_mom=_f(fr["beta_mom"]), beta_rmw=_f(fr["beta_rmw"]), beta_cma=_f(fr["beta_cma"]),
        t_mkt=_f(fr["t_mkt"]), t_smb=_f(fr["t_smb"]), t_hml=_f(fr["t_hml"]),
        t_mom=_f(fr["t_mom"]), t_rmw=_f(fr["t_rmw"]), t_cma=_f(fr["t_cma"]),
        r2=_f(fr["r2"]), adj_r2=_f(fr["adj_r2"]),
    ) for fr in factor_rows]

    display_name = row["clean_name"] or _clean_name(row["full_name"])

    return EtfDetail(
        isin=row["isin"],
        full_name=row["full_name"],
        display_name=display_name,
        short_name=row["short_name"],
        issuer_name=row["issuer_name"],
        fund_family=row["fund_family"],
        index_tracked=row["index_tracked"],
        asset_class=ac,
        replication_method=row["replication_method"],
        ter_pct=float(row["ter_pct"]) if row["ter_pct"] is not None else None,
        aum_eur=float(row["aum_eur"]) if row["aum_eur"] is not None else None,
        sfdr_article=row["sfdr_article"],
        fund_currency=row["fund_currency"],
        inception_date=row["inception_date"].isoformat() if row["inception_date"] else None,
        is_active=bool(row["is_active"]),
        price_series=[EtfPricePoint(date=d.isoformat(), close=c) for d, c in window_prices],
        period=period,
        return_1y=risk.return_1y if risk else _return_1y(window_prices),
        regime_score=score,
        risk_metrics=risk,
        regime_performance=perf,
        profile=profile_obj if has_profile else None,
        factors=factors,
        factor_performance=factor_performance_rows,
    )


# ---------------------------------------------------------------------------
# /{isin}/story  - AI Story (WA0007 §7)
# ---------------------------------------------------------------------------

_DISCLAIMER = {
    "en": "This is not investment advice. Past regime performance does not guarantee future results.",
    "de": "Dies ist keine Anlageberatung. Vergangene Regime-Performance ist keine Garantie für zukünftige Ergebnisse.",
}


def _story_system_prompt(lang: Lang) -> str:
    if lang == "de":
        return (
            "Du bist der MZQA-Anlageanalyst. Schreibe eine kurze, ehrliche Erklärung "
            "in genau 4 Absätzen, jeweils 2-3 Sätze, insgesamt 150-250 Wörter. "
            "Absatz 1: Makro-Kontext (aktuelles Regime, was das für die Anlageklasse bedeutet). "
            "Absatz 2: Wie passt dieser spezifische ETF zum aktuellen Regime? "
            "Absatz 3: Historische Belege (wie hat dieser Faktor / diese Anlageklasse in ähnlichen Regimen abgeschnitten?). "
            "Absatz 4: Risikohinweis - was könnte schiefgehen? "
            "Verwende einfaches Deutsch, keine Kursziele, nie 'kaufen' sagen, immer den Risikoabsatz einschließen."
        )
    return (
        "You are the MZQA investment analyst. Write a short, honest explanation "
        "in exactly 4 paragraphs, 2-3 sentences each, 150-250 words total. "
        "Paragraph 1: Macro context (current regime, what it means for the asset class). "
        "Paragraph 2: Why this specific ETF fits or does not fit the current regime. "
        "Paragraph 3: Historical evidence (how has this factor/asset class performed in similar regimes?). "
        "Paragraph 4: Honest risk caveat - what could go wrong? "
        "Plain language. No price targets. Never say 'buy'. Always include the risk paragraph."
    )


def _story_user_prompt(etf: EtfDetail, regime: str | None, lang: Lang) -> str:
    facts = {
        "isin": etf.isin, "name": etf.display_name, "asset_class": etf.asset_class,
        "index": etf.index_tracked, "ter_pct": etf.ter_pct, "aum_eur": etf.aum_eur,
        "sfdr_article": etf.sfdr_article, "replication": etf.replication_method,
        "fund_currency": etf.fund_currency, "return_1y": etf.return_1y,
        "regime": regime, "regime_score": etf.regime_score,
    }
    intro = "Use these facts. If a field is null, do not invent a value." if lang == "en" else \
            "Verwende diese Fakten. Erfinde keinen Wert, wenn ein Feld null ist."
    return f"{intro}\n\n{json.dumps(facts, ensure_ascii=False, default=str)}"


def _fallback_story(etf: EtfDetail, regime: str | None, lang: Lang) -> str:
    """Deterministic 4-paragraph story used when DeepSeek is unavailable or
    returns empty content (reasoning models can exhaust the token budget before
    emitting the final answer)."""
    rg = regime or "Late-cycle"
    if lang == "de":
        a = f"Wir befinden uns im Regime '{rg}'. In dieser Phase bevorzugen Investoren historisch defensive und qualitätsorientierte Engagements gegenüber höher-volatilen Wachstumstiteln."
        b = f"{etf.display_name} (ISIN {etf.isin}) bildet {etf.index_tracked or 'einen breiten Index'} ab und gehört zur Anlageklasse '{etf.asset_class or 'Aktien'}'. Mit einer TER von {(etf.ter_pct or 0)*100:.2f}% und einem Volumen von {(etf.aum_eur or 0)/1e9:.1f} Mrd. EUR liegt der Fonds in einem soliden Kosten- und Liquiditätsbereich."
        c = "Historisch hat diese Anlageklasse in vergleichbaren Phasen gemischte Ergebnisse geliefert; Qualitäts- und Value-Faktoren tendieren dazu, Wachstumstitel zu schlagen."
        d = "Risiken: Bei einem Regime-Wechsel zu Rezession oder früher Expansion kann diese Positionierung unterperformen. Konzentrations-, Währungs- und Liquiditätsrisiken sind zu beachten."
    else:
        a = f"The current macro regime is {rg}. This phase typically favours defensive and quality-tilted exposures over higher-beta growth assets."
        b = f"{etf.display_name} (ISIN {etf.isin}) tracks {etf.index_tracked or 'a broad index'} — an {etf.asset_class or 'equity'} exposure. Its TER of {(etf.ter_pct or 0)*100:.2f}% and AUM of EUR {(etf.aum_eur or 0)/1e9:.1f}bn place it in a solid cost/liquidity tier."
        c = "Historically, this asset class has delivered mixed results in similar periods, with quality and value factors generally outperforming higher-beta growth exposures."
        d = "Risk: if the regime shifts to recession or to an early-expansion phase, this positioning may underperform. Concentration, currency and liquidity risks also apply."
    return "\n\n".join([a, b, c, d])


async def _generate_story(etf: EtfDetail, regime: str | None, lang: Lang) -> str:
    api_key = llm_runtime.resolve_env_key()
    if not api_key:
        return _fallback_story(etf, regime, lang)
    # provider defaults to AI_ANALYST_LLM_PROVIDER (else DeepSeek); model follows it.
    try:
        msg = await llm_runtime.chat_once(
            api_key=api_key,
            messages=[
                {"role": "system", "content": _story_system_prompt(lang)},
                {"role": "user", "content": _story_user_prompt(etf, regime, lang)},
            ],
            temperature=0.3,
            # Reasoning models (deepseek-v4-flash) consume tokens in
            # reasoning_content first; 2400 leaves comfortable room for the
            # 150-250 word answer afterwards.
            max_tokens=2400,
        )
    except Exception as exc:  # noqa: BLE001 - story is best-effort
        logger.warning("etf.story LLM call failed for %s: %s", etf.isin, exc)
        return _fallback_story(etf, regime, lang)
    text = (msg.get("content") or "").strip()
    return text or _fallback_story(etf, regime, lang)


@router.get("/{isin}/story", response_model=EtfStory)
async def story(isin: str, lang: Lang = Query("en")) -> EtfStory:
    isin = isin.strip().upper()
    etf = await detail(isin)  # 404 if unknown
    regime, regime_date = await _current_regime()
    cache_key = (isin, regime or "", lang)
    cached = _STORY_CACHE.get(cache_key)
    if cached and cached.regime_date == regime_date:
        return cached

    text = await _generate_story(etf, regime, lang)
    story_en = text if lang == "en" else None
    story_de = text if lang == "de" else None
    out = EtfStory(
        isin=isin, regime=regime, regime_date=regime_date,
        regime_score=etf.regime_score,
        story_en=story_en, story_de=story_de,
        generated_at=datetime.now(timezone.utc).isoformat(),
        disclaimer=_DISCLAIMER[lang],
        model=llm_providers.chat_model(None),
    )
    _STORY_CACHE[cache_key] = out
    return out
