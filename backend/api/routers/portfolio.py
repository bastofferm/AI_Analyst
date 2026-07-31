"""Portfolio snapshot endpoint.

`POST /api/portfolio/snapshot` — given a list of `{ticker, jurisdiction, weight}`
holdings, returns trading-terminal vitals computed from
`fact_prices_us` + `fact_prices_jp`:

- annualized expected return (historical mean of daily log returns × 252)
- annualized volatility
- Sharpe ratio
- max drawdown over the lookback window
- beta and Pearson correlation vs the chosen benchmark (default SPY)
- effective N = 1 / Σ wᵢ²
- equity curve (portfolio NAV vs benchmark NAV)
- per-ticker contributions

Daily series are aligned on the intersection of trading dates so we never
multiply on a day a name was missing. Forward-fill is intentionally NOT used —
silently inventing prices would distort vol & DD.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import acquire
from ..quant import optimize as qopt
from ..quant import risk as qrisk

router = APIRouter()
logger = logging.getLogger("mzqa.portfolio")


class Holding(BaseModel):
    ticker: str
    jurisdiction: Literal["US", "JP"]
    weight: float | None = None  # equal-weighted when omitted


class SnapshotRequest(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    benchmark: str = "SPY"
    lookback_months: int = Field(default=12, ge=1, le=120)
    risk_free_annual: float = 0.045


class SummaryStats(BaseModel):
    expected_return_annual: float | None
    vol_annual: float | None
    sharpe: float | None
    max_drawdown_12m: float | None
    max_drawdown_date: str | None
    beta_vs_bench: float | None
    corr_vs_bench: float | None
    effective_n: float | None


class CurvePoint(BaseModel):
    date: str
    nav: float
    bench_nav: float


class Contribution(BaseModel):
    ticker: str
    weight: float
    ret_annual: float | None
    vol_contrib: float | None
    beta_contrib: float | None


class SnapshotResponse(BaseModel):
    summary: SummaryStats
    equity_curve: list[CurvePoint]
    contributions: list[Contribution]
    warnings: list[str]
    as_of: str | None
    lookback_start: str | None


# =========================================================================
# Consumer portfolio v1: identifier-neutral ETF portfolio endpoints
# =========================================================================

AssetType = Literal["etf", "equity"]
IdentifierType = Literal["isin", "ticker"]


class AssetIdentifier(BaseModel):
    asset_type: AssetType = "etf"
    identifier_type: IdentifierType = "isin"
    isin: str | None = None
    ticker: str | None = None
    name: str | None = None


class AssetHoldingInput(AssetIdentifier):
    weight: float | None = None
    source: str | None = None


class AssetResolveRequest(BaseModel):
    query: str | None = None
    queries: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=50)


class ResolvedAsset(BaseModel):
    asset_id: str
    asset_type: AssetType
    identifier_type: IdentifierType
    isin: str | None = None
    ticker: str | None = None
    name: str
    issuer_name: str | None = None
    asset_class: str | None = None
    fund_currency: str | None = None
    data_status: str
    warnings: list[str] = Field(default_factory=list)


class AssetResolveResponse(BaseModel):
    assets: list[ResolvedAsset]
    warnings: list[str] = Field(default_factory=list)


class AssetBenchmark(BaseModel):
    identifier_type: IdentifierType = "ticker"
    isin: str | None = None
    ticker: str | None = None
    label: str | None = None


class AssetSnapshotRequest(BaseModel):
    holdings: list[AssetHoldingInput] = Field(default_factory=list)
    benchmark: AssetBenchmark | None = None
    lookback_months: int = Field(default=120, ge=1, le=240)
    risk_free_annual: float = 0.03


class AssetSummaryStats(BaseModel):
    expected_return_annual: float | None
    vol_annual: float | None
    sharpe: float | None
    max_drawdown: float | None
    max_drawdown_days: int | None
    max_drawdown_date: str | None
    beta_vs_benchmark: float | None
    corr_vs_benchmark: float | None
    effective_n: float | None


class AssetCurvePoint(BaseModel):
    date: str
    nav: float
    benchmark_nav: float | None = None


class AssetContribution(BaseModel):
    asset_id: str
    isin: str | None
    ticker: str | None
    name: str
    weight: float
    ret_annual: float | None
    vol_annual: float | None
    beta_contrib: float | None = None


class RiskMatrixPoint(BaseModel):
    asset_id: str
    label: str
    weight: float
    ret_annual: float | None
    vol_annual: float | None
    kind: Literal["asset", "portfolio"]


class StressPeriodResult(BaseModel):
    key: str
    label: str
    start: str
    end: str
    observations: int
    portfolio_return: float | None
    max_drawdown: float | None
    max_drawdown_days: int | None
    data_status: Literal["complete", "partial", "unavailable"]
    note: str | None = None


class LookthroughExposure(BaseModel):
    symbol: str | None
    name: str | None
    exposure: float
    source_count: int
    cumulative_exposure: float
    cik: str | None = None
    edinet_code: str | None = None
    logo_url: str | None = None


class LookthroughSummary(BaseModel):
    top_10pct: list[LookthroughExposure]
    known_top10_weight: float
    coverage_weight: float
    warnings: list[str]


class UnsupportedAsset(BaseModel):
    asset_type: AssetType
    identifier_type: IdentifierType
    isin: str | None = None
    ticker: str | None = None
    name: str | None = None
    reason: str


class AssetSnapshotResponse(BaseModel):
    assets: list[ResolvedAsset]
    summary: AssetSummaryStats
    equity_curve: list[AssetCurvePoint]
    contributions: list[AssetContribution]
    risk_matrix: list[RiskMatrixPoint]
    stress_periods: list[StressPeriodResult]
    lookthrough: LookthroughSummary
    warnings: list[str]
    unsupported_assets: list[UnsupportedAsset]
    benchmark: ResolvedAsset | None
    as_of: str | None
    lookback_start: str | None


class AssetOptimizeRequest(BaseModel):
    universe: list[AssetIdentifier] = Field(default_factory=list)
    benchmark: AssetBenchmark | None = None
    lookback_months: int = Field(default=60, ge=6, le=240)
    risk_model: Literal["sample", "ledoit_wolf"] = "ledoit_wolf"
    risk_free_annual: float = 0.03
    weight_max: float = Field(default=0.45, ge=0.01, le=1.0)


class AssetOptimizeWeight(BaseModel):
    asset_id: str
    isin: str
    ticker: str | None
    name: str
    weight: float


class AssetOptimizeResponse(BaseModel):
    weights: list[AssetOptimizeWeight]
    expected_return_annual: float | None
    vol_annual: float | None
    sharpe: float | None
    efficient_frontier: list[dict[str, float]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str]
    as_of: str | None
    lookback_start: str | None


class AssetStoryRequest(AssetSnapshotRequest):
    lang: Literal["en", "de"] = "de"


class AssetStoryResponse(BaseModel):
    story: str
    sections: list[dict[str, str]]
    disclaimer: str
    warnings: list[str]


class StarterMixHolding(BaseModel):
    asset: ResolvedAsset
    weight: float
    role: str
    ter_pct: float | None = None
    aum_eur: float | None = None
    return_1y: float | None = None
    volatility_annual: float | None = None
    stock_pct: float | None = None
    bond_pct: float | None = None
    price_points: int = 0
    price_start: str | None = None
    price_end: str | None = None


class StarterMix(BaseModel):
    slug: str
    name: str
    description: str
    maintenance: str
    risk_band: Literal["lower", "medium", "higher"]
    blended_ter: float | None = None
    equity_pct: float | None = None
    bond_pct: float | None = None
    holdings: list[StarterMixHolding]
    warnings: list[str] = Field(default_factory=list)


class StarterMixResponse(BaseModel):
    mixes: list[StarterMix]
    as_of: str | None = None
    warnings: list[str] = Field(default_factory=list)


_DEFAULT_BENCHMARKS = [
    AssetBenchmark(identifier_type="isin", isin="IE00B4L5Y983", label="MSCI World ETF"),
    AssetBenchmark(identifier_type="ticker", ticker="EUNL", label="MSCI World ETF"),
    AssetBenchmark(identifier_type="ticker", ticker="IWDA", label="MSCI World ETF"),
]

_STARTER_MIX_RECIPES: list[dict[str, Any]] = [
    {
        "slug": "simple-global",
        "name": "Global Core",
        "description": "A broad global equity core for investors who want a simple long-term ETF base.",
        "holdings": [
            {"isin": "IE00B6R52259", "weight": 1.0, "role": "MSCI ACWI core"},
        ],
    },
    {
        "slug": "us-core",
        "name": "US Focus",
        "description": "A low-cost US equity reference with a small bond stabilizer.",
        "holdings": [
            {"isin": "IE00B5BMR087", "weight": 0.85, "role": "US equity core"},
            {"isin": "IE00BDBRDM35", "weight": 0.15, "role": "Global bonds"},
        ],
    },
    {
        "slug": "europe-core",
        "name": "Europe Focus",
        "description": "A Europe-first ETF reference with global bonds as a volatility anchor.",
        "holdings": [
            {"isin": "IE00B4K48X80", "weight": 0.85, "role": "European equity core"},
            {"isin": "IE00BDBRDM35", "weight": 0.15, "role": "Global bonds"},
        ],
    },
    {
        "slug": "global-em-asia",
        "name": "Global + EM Asia",
        "description": "A global equity mix with explicit Emerging Markets and Asia Pacific exposure.",
        "holdings": [
            {"isin": "IE00B4L5Y983", "weight": 0.55, "role": "Developed markets core"},
            {"isin": "IE00BKM4GZ66", "weight": 0.25, "role": "Emerging markets"},
            {"isin": "IE00B9F5YL18", "weight": 0.20, "role": "Asia Pacific satellite"},
        ],
    },
    {
        "slug": "balanced",
        "name": "Balanced",
        "description": "A global equity core with a bond sleeve for a balanced long-term structure.",
        "holdings": [
            {"isin": "IE00B4L5Y983", "weight": 0.70, "role": "Global equity core"},
            {"isin": "IE00BDBRDM35", "weight": 0.30, "role": "Global bonds"},
        ],
    },
    {
        "slug": "multi-asset",
        "name": "Multi Asset",
        "description": "A diversified ETF reference across equities, fixed income and commodities.",
        "holdings": [
            {"isin": "IE00B6R52259", "weight": 0.55, "role": "Global equities"},
            {"isin": "IE00BDBRDM35", "weight": 0.30, "role": "Global fixed income"},
            {"isin": "IE00BDFL4P12", "weight": 0.15, "role": "Broad commodities"},
        ],
    },
    {
        "slug": "defensive",
        "name": "Defensive",
        "description": "A steadier ETF mix for investors who prefer lower equity exposure.",
        "holdings": [
            {"isin": "IE00B4L5Y983", "weight": 0.40, "role": "Global equity core"},
            {"isin": "IE00BDBRDM35", "weight": 0.60, "role": "Global bonds"},
        ],
    },
]

_STRESS_WINDOWS: list[tuple[str, str, date, date]] = [
    ("gfc_2008", "2008 Global Financial Crisis", date(2008, 1, 1), date(2009, 3, 31)),
    ("euro_2012", "2012 Eurozone stress", date(2012, 1, 1), date(2012, 12, 31)),
    ("covid_2020", "2020 Covid shock", date(2020, 2, 1), date(2020, 4, 30)),
    ("rates_2022", "2022 inflation and rates shock", date(2022, 1, 1), date(2022, 12, 31)),
]


def _clean_identifier(value: str | None) -> str:
    return (value or "").strip().upper()


def _asset_query_value(asset: AssetIdentifier | AssetBenchmark | AssetHoldingInput) -> str:
    return _clean_identifier(asset.isin if asset.identifier_type == "isin" else asset.ticker)


def _float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _starter_asset_class_mix(asset_class: str | None, stock_pct: float | None, bond_pct: float | None) -> tuple[float | None, float | None]:
    stock = _float(stock_pct)
    bond = _float(bond_pct)
    if stock is not None or bond is not None:
        return stock, bond
    cls = (asset_class or "").lower()
    if "fixed" in cls or "bond" in cls:
        return 0.0, 1.0
    if "mixed" in cls or "multi" in cls:
        return 0.5, 0.5
    if "commodity" in cls:
        return 0.0, 0.0
    return 1.0, 0.0


def _starter_risk_band(equity_pct: float | None) -> Literal["lower", "medium", "higher"]:
    equity = equity_pct if equity_pct is not None else 1.0
    if equity <= 0.45:
        return "lower"
    if equity <= 0.75:
        return "medium"
    return "higher"


def _starter_price_stats(rows: list[Any]) -> dict[str, Any]:
    prices = [(row["price_date"], _float(row["close"])) for row in rows]
    prices = [(d, c) for d, c in prices if c is not None and c > 0]
    if len(prices) < 2:
        return {
            "return_1y": None,
            "volatility_annual": None,
            "price_points": len(prices),
            "price_start": str(prices[0][0]) if prices else None,
            "price_end": str(prices[-1][0]) if prices else None,
        }
    closes = np.array([c for _, c in prices], dtype=float)
    returns = closes[1:] / closes[:-1] - 1.0
    vol = float(np.nanstd(returns, ddof=1) * np.sqrt(252.0)) if len(returns) >= 30 else None
    ret = float(closes[-1] / closes[0] - 1.0) if closes[0] > 0 else None
    return {
        "return_1y": ret if ret is not None and np.isfinite(ret) else None,
        "volatility_annual": vol if vol is not None and np.isfinite(vol) else None,
        "price_points": len(prices),
        "price_start": str(prices[0][0]),
        "price_end": str(prices[-1][0]),
    }


def _resolved_asset_from_row(row, *, identifier_type: IdentifierType) -> ResolvedAsset:
    ticker = row["yf_ticker"] or row["exchange_ticker"]
    warnings: list[str] = []
    status = row["profile_status"] or "profile_missing"
    if row["profile_status"] != "complete":
        warnings.append("ETF profile is incomplete; holdings and look-through may be limited.")
    return ResolvedAsset(
        asset_id=f"etf:{row['isin']}",
        asset_type="etf",
        identifier_type=identifier_type,
        isin=row["isin"],
        ticker=ticker,
        name=row["clean_name"] or row["full_name"],
        issuer_name=row["issuer_name"],
        asset_class=row["asset_class"],
        fund_currency=row["fund_currency"],
        data_status=status,
        warnings=warnings,
    )


async def _resolve_one_etf(conn, asset: AssetIdentifier | AssetBenchmark | AssetHoldingInput) -> ResolvedAsset | None:
    query = _asset_query_value(asset)
    if not query:
        return None
    row = await conn.fetchrow(
        """
        SELECT d.isin, d.full_name, d.issuer_name, d.asset_class, d.fund_currency,
               p.clean_name, p.yf_ticker, p.profile_status,
               (SELECT l.exchange_ticker
                FROM sec.dim_etf_listing l
                WHERE l.isin = d.isin AND l.exchange_ticker IS NOT NULL
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic
                LIMIT 1) AS exchange_ticker
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE upper(d.isin) = $1
           OR upper(COALESCE(p.yf_ticker, '')) = $1
           OR EXISTS (
                SELECT 1 FROM sec.dim_etf_listing l
                WHERE l.isin = d.isin AND upper(COALESCE(l.exchange_ticker, '')) = $1
           )
        ORDER BY CASE
            WHEN upper(d.isin) = $1 THEN 0
            WHEN upper(COALESCE(p.yf_ticker, '')) = $1 THEN 1
            ELSE 2
        END
        LIMIT 1
        """,
        query,
    )
    if not row:
        return None
    return _resolved_asset_from_row(row, identifier_type=asset.identifier_type)


async def _search_etf_assets(conn, raw_query: str, limit: int) -> list[ResolvedAsset]:
    query = raw_query.strip()
    if not query:
        return []
    like = f"%{query}%"
    exact = query.upper()
    rows = await conn.fetch(
        """
        SELECT d.isin, d.full_name, d.issuer_name, d.asset_class, d.fund_currency,
               p.clean_name, p.yf_ticker, p.profile_status,
               (SELECT l.exchange_ticker
                FROM sec.dim_etf_listing l
                WHERE l.isin = d.isin AND l.exchange_ticker IS NOT NULL
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic
                LIMIT 1) AS exchange_ticker
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE upper(d.isin) = $1
           OR upper(COALESCE(p.yf_ticker, '')) = $1
           OR d.full_name ILIKE $2
           OR p.clean_name ILIKE $2
           OR d.index_tracked ILIKE $2
           OR EXISTS (
                SELECT 1 FROM sec.dim_etf_listing l
                WHERE l.isin = d.isin AND upper(COALESCE(l.exchange_ticker, '')) = $1
           )
        ORDER BY CASE
            WHEN upper(d.isin) = $1 THEN 0
            WHEN upper(COALESCE(p.yf_ticker, '')) = $1 THEN 1
            WHEN p.clean_name ILIKE $2 THEN 2
            ELSE 3
        END, COALESCE(d.aum_eur, 0) DESC
        LIMIT $3
        """,
        exact,
        like,
        limit,
    )
    return [_resolved_asset_from_row(row, identifier_type="isin" if row["isin"].upper() == exact else "ticker") for row in rows]


async def _resolve_assets_for_holdings(
    conn,
    holdings: list[AssetHoldingInput] | list[AssetIdentifier],
) -> tuple[list[ResolvedAsset], list[UnsupportedAsset], list[str]]:
    assets: list[ResolvedAsset] = []
    unsupported: list[UnsupportedAsset] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in holdings:
        if item.asset_type == "equity":
            unsupported.append(UnsupportedAsset(
                asset_type=item.asset_type,
                identifier_type=item.identifier_type,
                isin=item.isin,
                ticker=item.ticker,
                name=item.name,
                reason="Equity identifiers are stored in the v1 model but are not yet priced by the ETF portfolio engine.",
            ))
            continue
        resolved = await _resolve_one_etf(conn, item)
        if resolved is None:
            unsupported.append(UnsupportedAsset(
                asset_type=item.asset_type,
                identifier_type=item.identifier_type,
                isin=item.isin,
                ticker=item.ticker,
                name=item.name,
                reason="No ETF match found for this ISIN/ticker.",
            ))
            continue
        if resolved.asset_id in seen:
            warnings.append(f"Duplicate ETF ignored: {resolved.name} ({resolved.isin})")
            continue
        seen.add(resolved.asset_id)
        assets.append(resolved)
    return assets, unsupported, warnings


async def _starter_mix_holding(conn, spec: dict[str, Any]) -> tuple[StarterMixHolding | None, str | None]:
    isin = _clean_identifier(spec.get("isin"))
    if not isin:
        return None, "Starter mix recipe is missing an ISIN."
    row = await conn.fetchrow(
        """
        SELECT d.isin, d.full_name, d.issuer_name, d.asset_class, d.fund_currency,
               d.ter_pct, d.aum_eur,
               p.clean_name, p.yf_ticker, p.profile_status, p.stock_pct, p.bond_pct,
               (SELECT l.exchange_ticker
                FROM sec.dim_etf_listing l
                WHERE l.isin = d.isin AND l.exchange_ticker IS NOT NULL
                ORDER BY l.is_primary_listing DESC, (l.mic='XETR') DESC, l.mic
                LIMIT 1) AS exchange_ticker
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE upper(d.isin) = $1
        """,
        isin,
    )
    if not row:
        return None, f"Starter mix ETF not found in warehouse: {isin}"
    price_rows = await conn.fetch(
        """
        SELECT price_date, close
        FROM sec.fact_prices_etf
        WHERE isin = $1
          AND close IS NOT NULL
          AND price_date >= CURRENT_DATE - INTERVAL '400 days'
        ORDER BY price_date
        """,
        isin,
    )
    stats = _starter_price_stats(price_rows)
    stock_pct, bond_pct = _starter_asset_class_mix(
        row["asset_class"],
        _float(row["stock_pct"]),
        _float(row["bond_pct"]),
    )
    asset = _resolved_asset_from_row(row, identifier_type="isin")
    return StarterMixHolding(
        asset=asset,
        weight=float(spec.get("weight") or 0.0),
        role=str(spec.get("role") or "Portfolio building block"),
        ter_pct=_float(row["ter_pct"]),
        aum_eur=_float(row["aum_eur"]),
        return_1y=stats["return_1y"],
        volatility_annual=stats["volatility_annual"],
        stock_pct=stock_pct,
        bond_pct=bond_pct,
        price_points=int(stats["price_points"]),
        price_start=stats["price_start"],
        price_end=stats["price_end"],
    ), None


def _build_starter_mix(recipe: dict[str, Any], holdings: list[StarterMixHolding], warnings: list[str]) -> StarterMix:
    total = sum(max(0.0, h.weight) for h in holdings)
    normalized = holdings
    if total > 0 and abs(total - 1.0) > 0.005:
        normalized = [h.model_copy(update={"weight": h.weight / total}) for h in holdings]
        warnings = [*warnings, f"Weights for {recipe['name']} summed to {total:.3f}; normalized to 1.0."]

    weighted_ter = 0.0
    ter_weight = 0.0
    equity = 0.0
    bond = 0.0
    allocation_weight = 0.0
    as_of_dates = [h.price_end for h in normalized if h.price_end]
    for holding in normalized:
        if holding.ter_pct is not None:
            weighted_ter += holding.weight * holding.ter_pct
            ter_weight += holding.weight
        if holding.stock_pct is not None or holding.bond_pct is not None:
            equity += holding.weight * (holding.stock_pct or 0.0)
            bond += holding.weight * (holding.bond_pct or 0.0)
            allocation_weight += holding.weight
    equity_pct = equity / allocation_weight if allocation_weight > 0 else None
    bond_pct = bond / allocation_weight if allocation_weight > 0 else None
    if any(h.price_points < 30 for h in normalized):
        warnings = [*warnings, "One or more holdings have limited price history; risk figures may be sparse."]
    return StarterMix(
        slug=str(recipe["slug"]),
        name=str(recipe["name"]),
        description=str(recipe["description"]),
        maintenance=str(recipe.get("maintenance", "Reference baseline")),
        risk_band=_starter_risk_band(equity_pct),
        blended_ter=(weighted_ter / ter_weight) if ter_weight > 0 else None,
        equity_pct=equity_pct,
        bond_pct=bond_pct,
        holdings=normalized,
        warnings=warnings,
    )


async def _fetch_etf_series(conn, isins: list[str], start: date, end: date) -> dict[str, dict[date, float]]:
    if not isins:
        return {}
    rows = await conn.fetch(
        """
        WITH ranked AS (
            SELECT p.isin, p.price_date, p.close,
                   row_number() OVER (
                     PARTITION BY p.isin, p.price_date
                     ORDER BY COALESCE(l.is_primary_listing, false) DESC,
                              (p.mic = 'XETR') DESC,
                              p.mic
                   ) AS rn
            FROM sec.fact_prices_etf p
            LEFT JOIN sec.dim_etf_listing l ON l.isin = p.isin AND l.mic = p.mic
            WHERE p.isin = ANY($1::text[])
              AND p.price_date BETWEEN $2 AND $3
              AND p.close IS NOT NULL
        )
        SELECT isin, price_date, close
        FROM ranked
        WHERE rn = 1
        ORDER BY isin, price_date
        """,
        isins,
        start,
        end,
    )
    series: dict[str, dict[date, float]] = {}
    for row in rows:
        series.setdefault(row["isin"], {})[row["price_date"]] = float(row["close"])
    return series


def _normalize_weights(raw_weights: list[float | None]) -> tuple[np.ndarray, list[str]]:
    warnings: list[str] = []
    if not raw_weights:
        return np.array([], dtype=float), warnings
    n = len(raw_weights)
    weights = np.array([w if w is not None else 1.0 / n for w in raw_weights], dtype=float)
    total = float(np.sum(weights))
    if total <= 0:
        raise HTTPException(status_code=400, detail="Weights sum to zero.")
    if abs(total - 1.0) > 0.005:
        warnings.append(f"Weights summed to {total:.3f}; normalized to 1.0.")
    return weights / total, warnings


def _max_drawdown(nav: np.ndarray, nav_dates: list[date]) -> tuple[float | None, int | None, str | None]:
    if len(nav) < 2:
        return None, None, None
    peak = float(nav[0])
    peak_date = nav_dates[0]
    worst = 0.0
    worst_days = 0
    worst_date: date | None = None
    for value, d in zip(nav, nav_dates):
        v = float(value)
        if v > peak:
            peak = v
            peak_date = d
        dd = v / peak - 1.0 if peak > 0 else 0.0
        if dd < worst:
            worst = dd
            worst_days = max(0, (d - peak_date).days)
            worst_date = d
    return (worst if worst < 0 else None, worst_days if worst < 0 else None, str(worst_date) if worst_date else None)


def _downsample_curve(dates: list[date], nav: np.ndarray, bench_nav: np.ndarray | None) -> list[AssetCurvePoint]:
    if not dates:
        return []
    step = max(1, len(dates) // 140)
    out: list[AssetCurvePoint] = []
    for i in range(0, len(dates), step):
        out.append(AssetCurvePoint(
            date=str(dates[i]),
            nav=float(nav[i]),
            benchmark_nav=float(bench_nav[i]) if bench_nav is not None else None,
        ))
    if out and out[-1].date != str(dates[-1]):
        out.append(AssetCurvePoint(
            date=str(dates[-1]),
            nav=float(nav[-1]),
            benchmark_nav=float(bench_nav[-1]) if bench_nav is not None else None,
        ))
    return out


def _stress_results(dates: list[date], nav: np.ndarray) -> list[StressPeriodResult]:
    out: list[StressPeriodResult] = []
    for key, label, start, end in _STRESS_WINDOWS:
        idx = [i for i, d in enumerate(dates) if start <= d <= end]
        if len(idx) < 2:
            out.append(StressPeriodResult(
                key=key,
                label=label,
                start=str(start),
                end=str(end),
                observations=len(idx),
                portfolio_return=None,
                max_drawdown=None,
                max_drawdown_days=None,
                data_status="unavailable",
                note="No overlapping ETF price history for this stress period.",
            ))
            continue
        sub_nav = nav[idx]
        sub_dates = [dates[i] for i in idx]
        period_return = float(sub_nav[-1] / sub_nav[0] - 1.0) if sub_nav[0] > 0 else None
        dd, dd_days, _ = _max_drawdown(sub_nav, sub_dates)
        coverage_days = (min(end, sub_dates[-1]) - max(start, sub_dates[0])).days
        full_days = max(1, (end - start).days)
        status: Literal["complete", "partial", "unavailable"] = "complete" if coverage_days / full_days > 0.85 else "partial"
        out.append(StressPeriodResult(
            key=key,
            label=label,
            start=str(start),
            end=str(end),
            observations=len(idx),
            portfolio_return=period_return,
            max_drawdown=dd,
            max_drawdown_days=dd_days,
            data_status=status,
            note=None if status == "complete" else "Only partial ETF price history overlaps this period.",
        ))
    return out


async def _portfolio_lookthrough(
    conn,
    assets: list[ResolvedAsset],
    weights: np.ndarray,
) -> LookthroughSummary:
    warnings: list[str] = []
    if not assets:
        return LookthroughSummary(top_10pct=[], known_top10_weight=0.0, coverage_weight=0.0, warnings=warnings)
    isins = [asset.isin for asset in assets if asset.isin]
    profile_rows = await conn.fetch(
        """
        SELECT d.isin, d.asset_class, p.stock_pct, p.bond_pct, p.other_pct,
               p.holdings_count, p.profile_status
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE d.isin = ANY($1::text[])
        """,
        isins,
    )
    profiles = {row["isin"]: row for row in profile_rows}
    holding_rows = await conn.fetch(
        """
        SELECT isin, symbol, name, weight, cik, edinet_code, logo_url
        FROM sec.etf_holding
        WHERE isin = ANY($1::text[])
        ORDER BY isin, rank
        """,
        isins,
    )
    by_isin: dict[str, list[Any]] = {}
    for row in holding_rows:
        by_isin.setdefault(row["isin"], []).append(row)

    aggregate: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    known_top10_weight = 0.0
    coverage_weight = 0.0
    for asset, weight in zip(assets, weights):
        isin = asset.isin
        if not isin:
            continue
        profile = profiles.get(isin)
        rows = by_isin.get(isin, [])
        if rows:
            coverage_weight += float(weight)
            known_top10_weight += float(weight) * sum(max(0.0, _float(row["weight"]) or 0.0) for row in rows)
        else:
            warnings.append(f"No top-10 holdings available for {asset.name}; look-through is incomplete.")
        if profile:
            asset_class = (profile["asset_class"] or "").lower()
            stock_pct = _float(profile["stock_pct"])
            bond_pct = _float(profile["bond_pct"])
            other_pct = _float(profile["other_pct"])
            if asset_class in {"fixed income", "mixed"} or (bond_pct or 0.0) >= 0.2 or (other_pct or 0.0) >= 0.2 or (stock_pct is not None and stock_pct < 0.6):
                warnings.append(f"{asset.name} is not a clean equity ETF; top-10 equity look-through may understate the real exposure.")
            if profile["profile_status"] != "complete":
                warnings.append(f"Profile data for {asset.name} is marked '{profile['profile_status'] or 'missing'}'.")
        for row in rows:
            h_weight = _float(row["weight"])
            if h_weight is None or h_weight <= 0:
                continue
            key = (row["symbol"], row["name"])
            item = aggregate.setdefault(key, {
                "symbol": row["symbol"],
                "name": row["name"],
                "exposure": 0.0,
                "source_count": 0,
                "cik": row["cik"],
                "edinet_code": row["edinet_code"],
                "logo_url": row["logo_url"],
            })
            item["exposure"] += float(weight) * h_weight
            item["source_count"] += 1

    cumulative = 0.0
    top: list[LookthroughExposure] = []
    for item in sorted(aggregate.values(), key=lambda x: x["exposure"], reverse=True):
        if cumulative >= 0.10 and top:
            break
        cumulative += float(item["exposure"])
        top.append(LookthroughExposure(
            symbol=item["symbol"],
            name=item["name"],
            exposure=float(item["exposure"]),
            source_count=int(item["source_count"]),
            cumulative_exposure=cumulative,
            cik=item.get("cik"),
            edinet_code=item.get("edinet_code"),
            logo_url=item.get("logo_url"),
        ))
    return LookthroughSummary(
        top_10pct=top,
        known_top10_weight=known_top10_weight,
        coverage_weight=coverage_weight,
        warnings=warnings,
    )


async def _resolve_benchmark(conn, requested: AssetBenchmark | None) -> ResolvedAsset | None:
    candidates = [requested] if requested else []
    candidates += _DEFAULT_BENCHMARKS
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = await _resolve_one_etf(conn, candidate)
        if resolved is not None:
            return resolved
    return None


async def _build_asset_snapshot(req: AssetSnapshotRequest) -> AssetSnapshotResponse:
    if not req.holdings:
        raise HTTPException(status_code=400, detail="No holdings supplied.")

    warnings: list[str] = []
    today = date.today()
    start = today - timedelta(days=req.lookback_months * 31 + 14)

    async with acquire() as conn:
        assets, unsupported, resolve_warnings = await _resolve_assets_for_holdings(conn, req.holdings)
        warnings.extend(resolve_warnings)
        if not assets:
            raise HTTPException(status_code=400, detail="No supported ETF holdings could be resolved.")
        asset_ids = {asset.asset_id for asset in assets}
        raw_by_asset: dict[str, float | None] = {asset.asset_id: None for asset in assets}
        for holding in req.holdings:
            if holding.asset_type != "etf":
                continue
            resolved_for_weight = await _resolve_one_etf(conn, holding)
            if resolved_for_weight and resolved_for_weight.asset_id in asset_ids and raw_by_asset.get(resolved_for_weight.asset_id) is None:
                raw_by_asset[resolved_for_weight.asset_id] = holding.weight
        weights, weight_warnings = _normalize_weights([raw_by_asset.get(asset.asset_id) for asset in assets])
        warnings.extend(weight_warnings)
        benchmark = await _resolve_benchmark(conn, req.benchmark)
        series = await _fetch_etf_series(conn, [asset.isin for asset in assets if asset.isin], start, today)
        benchmark_series = await _fetch_etf_series(conn, [benchmark.isin] if benchmark and benchmark.isin else [], start, today)
        lookthrough = await _portfolio_lookthrough(conn, assets, weights)
    warnings.extend(lookthrough.warnings)

    keep_idx = [i for i, asset in enumerate(assets) if asset.isin and series.get(asset.isin)]
    dropped = [assets[i].name for i in range(len(assets)) if i not in keep_idx]
    if dropped:
        warnings.append(f"Missing ETF price history for {len(dropped)} asset(s); dropped: {', '.join(dropped[:4])}.")
    if not keep_idx:
        empty_summary = AssetSummaryStats(
            expected_return_annual=None, vol_annual=None, sharpe=None,
            max_drawdown=None, max_drawdown_days=None, max_drawdown_date=None,
            beta_vs_benchmark=None, corr_vs_benchmark=None, effective_n=None,
        )
        return AssetSnapshotResponse(
            assets=assets,
            summary=empty_summary,
            equity_curve=[],
            contributions=[],
            risk_matrix=[],
            stress_periods=[],
            lookthrough=lookthrough,
            warnings=warnings,
            unsupported_assets=unsupported,
            benchmark=benchmark,
            as_of=None,
            lookback_start=None,
        )

    assets = [assets[i] for i in keep_idx]
    weights = weights[keep_idx]
    weights = weights / weights.sum()
    series_map = {asset.isin: series[asset.isin] for asset in assets if asset.isin}
    bench_key = benchmark.isin if benchmark and benchmark.isin and benchmark_series.get(benchmark.isin) else None
    if bench_key:
        series_map["__benchmark__"] = benchmark_series[bench_key]
    elif benchmark is not None:
        warnings.append(f"Benchmark {benchmark.name} has no overlapping ETF price history; beta is unavailable.")

    dates, arrays = _align(series_map)
    if len(dates) < 30:
        warnings.append(f"Only {len(dates)} overlapping price observations; results may be noisy.")
    if len(dates) < 2:
        raise HTTPException(status_code=503, detail="Not enough overlapping ETF price history.")

    P = np.column_stack([arrays[asset.isin] for asset in assets if asset.isin])
    R = P[1:] / P[:-1] - 1.0
    port_ret = R @ weights
    port_nav = np.concatenate(([1.0], np.cumprod(1.0 + port_ret)))
    mu_annual = float(np.nanmean(port_ret)) * 252.0
    vol_annual = float(np.nanstd(port_ret, ddof=1)) * np.sqrt(252.0) if len(port_ret) > 1 else None
    sharpe = (mu_annual - req.risk_free_annual) / vol_annual if vol_annual and vol_annual > 0 else None
    max_dd, max_dd_days, max_dd_date = _max_drawdown(port_nav, dates)
    effective_n = float(1.0 / np.sum(weights**2)) if np.sum(weights**2) > 0 else None

    beta = corr = None
    bench_nav = None
    if "__benchmark__" in arrays:
        B = arrays["__benchmark__"]
        Rb = B[1:] / B[:-1] - 1.0
        bench_nav = np.concatenate(([1.0], np.cumprod(1.0 + Rb)))
        var_b = float(np.var(Rb, ddof=1)) if len(Rb) > 1 else 0.0
        if var_b > 0 and len(Rb) == len(port_ret):
            beta = float(np.cov(port_ret, Rb, ddof=1)[0, 1] / var_b)
            corr = float(np.corrcoef(port_ret, Rb)[0, 1])

    contributions: list[AssetContribution] = []
    risk_matrix: list[RiskMatrixPoint] = []
    for i, asset in enumerate(assets):
        ri = R[:, i]
        ret_i = float(np.nanmean(ri)) * 252.0 if len(ri) else None
        vol_i = float(np.nanstd(ri, ddof=1)) * np.sqrt(252.0) if len(ri) > 1 else None
        beta_contrib = None
        if "__benchmark__" in arrays and beta is not None:
            Rb = arrays["__benchmark__"][1:] / arrays["__benchmark__"][:-1] - 1.0
            var_b = float(np.var(Rb, ddof=1)) if len(Rb) > 1 else 0.0
            if var_b > 0:
                beta_contrib = float(weights[i] * (np.cov(ri, Rb, ddof=1)[0, 1] / var_b))
        contributions.append(AssetContribution(
            asset_id=asset.asset_id,
            isin=asset.isin,
            ticker=asset.ticker,
            name=asset.name,
            weight=float(weights[i]),
            ret_annual=ret_i,
            vol_annual=vol_i,
            beta_contrib=beta_contrib,
        ))
        risk_matrix.append(RiskMatrixPoint(
            asset_id=asset.asset_id,
            label=asset.ticker or asset.isin or asset.name,
            weight=float(weights[i]),
            ret_annual=ret_i,
            vol_annual=vol_i,
            kind="asset",
        ))
    risk_matrix.append(RiskMatrixPoint(
        asset_id="portfolio",
        label="Portfolio",
        weight=1.0,
        ret_annual=mu_annual,
        vol_annual=vol_annual,
        kind="portfolio",
    ))

    return AssetSnapshotResponse(
        assets=assets,
        summary=AssetSummaryStats(
            expected_return_annual=mu_annual if np.isfinite(mu_annual) else None,
            vol_annual=vol_annual if vol_annual is not None and np.isfinite(vol_annual) else None,
            sharpe=sharpe if sharpe is not None and np.isfinite(sharpe) else None,
            max_drawdown=max_dd,
            max_drawdown_days=max_dd_days,
            max_drawdown_date=max_dd_date,
            beta_vs_benchmark=beta if beta is not None and np.isfinite(beta) else None,
            corr_vs_benchmark=corr if corr is not None and np.isfinite(corr) else None,
            effective_n=effective_n,
        ),
        equity_curve=_downsample_curve(dates, port_nav, bench_nav),
        contributions=contributions,
        risk_matrix=risk_matrix,
        stress_periods=_stress_results(dates, port_nav),
        lookthrough=lookthrough,
        warnings=warnings,
        unsupported_assets=unsupported,
        benchmark=benchmark,
        as_of=str(dates[-1]) if dates else None,
        lookback_start=str(dates[0]) if dates else None,
    )


@router.get("/assets/starter-mixes", response_model=StarterMixResponse)
async def starter_mixes() -> StarterMixResponse:
    mixes: list[StarterMix] = []
    global_warnings: list[str] = []
    as_of_candidates: list[str] = []
    async with acquire() as conn:
        for recipe in _STARTER_MIX_RECIPES:
            holdings: list[StarterMixHolding] = []
            warnings: list[str] = []
            for spec in recipe["holdings"]:
                holding, warning = await _starter_mix_holding(conn, spec)
                if warning:
                    warnings.append(warning)
                if holding:
                    holdings.append(holding)
                    if holding.price_end:
                        as_of_candidates.append(holding.price_end)
            if holdings:
                mixes.append(_build_starter_mix(recipe, holdings, warnings))
            else:
                global_warnings.append(f"Starter mix unavailable: {recipe['name']}.")
    return StarterMixResponse(
        mixes=mixes,
        as_of=max(as_of_candidates) if as_of_candidates else None,
        warnings=global_warnings,
    )


@router.post("/assets/resolve", response_model=AssetResolveResponse)
async def resolve_assets(req: AssetResolveRequest) -> AssetResolveResponse:
    warnings: list[str] = []
    queries = [q for q in ([req.query] if req.query else []) + req.queries if q and q.strip()]
    if not queries:
        return AssetResolveResponse(assets=[], warnings=["No query supplied."])
    out: list[ResolvedAsset] = []
    seen: set[str] = set()
    async with acquire() as conn:
        for query in queries:
            rows = await _search_etf_assets(conn, query, req.limit)
            for asset in rows:
                if asset.asset_id in seen:
                    continue
                seen.add(asset.asset_id)
                out.append(asset)
                if len(out) >= req.limit:
                    break
            if len(out) >= req.limit:
                break
    if not out:
        warnings.append("No ETF match found. Equity tickers are reserved for a later mixed-asset engine.")
    return AssetResolveResponse(assets=out, warnings=warnings)


@router.post("/assets/snapshot", response_model=AssetSnapshotResponse)
async def asset_snapshot(req: AssetSnapshotRequest) -> AssetSnapshotResponse:
    return await _build_asset_snapshot(req)


@router.post("/assets/optimize", response_model=AssetOptimizeResponse)
async def asset_optimize(req: AssetOptimizeRequest) -> AssetOptimizeResponse:
    if not req.universe:
        raise HTTPException(status_code=400, detail="Universe is empty.")
    warnings: list[str] = []
    today = date.today()
    start = today - timedelta(days=req.lookback_months * 31 + 14)
    async with acquire() as conn:
        assets, unsupported, resolve_warnings = await _resolve_assets_for_holdings(conn, req.universe)
        warnings.extend(resolve_warnings)
        if unsupported:
            warnings.append(f"{len(unsupported)} unsupported asset(s) excluded from optimization.")
        if len(assets) < 2:
            raise HTTPException(status_code=400, detail="At least two supported ETF assets are required.")
        series = await _fetch_etf_series(conn, [asset.isin for asset in assets if asset.isin], start, today)
    kept_assets = [asset for asset in assets if asset.isin and series.get(asset.isin)]
    dropped = [asset.name for asset in assets if not asset.isin or not series.get(asset.isin)]
    if dropped:
        warnings.append(f"Dropped ETF(s) with missing price history: {', '.join(dropped[:5])}.")
    if len(kept_assets) < 2:
        raise HTTPException(status_code=503, detail="Need at least two ETFs with overlapping price history.")
    dates, arrays = _align({asset.isin: series[asset.isin] for asset in kept_assets if asset.isin})
    if len(dates) < 30:
        raise HTTPException(status_code=503, detail=f"Only {len(dates)} overlapping observations; need at least 30.")
    P = np.column_stack([arrays[asset.isin] for asset in kept_assets if asset.isin])
    R = P[1:] / P[:-1] - 1.0
    mu_daily = R.mean(axis=0)
    bundle = qrisk.build_risk_bundle(
        tickers=[asset.isin or asset.asset_id for asset in kept_assets],
        R=R,
        mu_daily=mu_daily,
        model=req.risk_model,
        loadings=None,
        residual_vol_daily=None,
        factor_panel=None,
        factor_names=None,
    )
    effective_weight_max = req.weight_max
    min_feasible_cap = 1.0 / len(kept_assets)
    if effective_weight_max < min_feasible_cap:
        effective_weight_max = min_feasible_cap
        warnings.append(f"weight_max raised to {effective_weight_max:.3f} so the ETF universe is feasible.")
    inputs = qopt.OptimizeRequestInputs(
        tickers=bundle.tickers,
        mu=bundle.mu,
        sigma=bundle.sigma,
        B=None,
        factor_names=[],
        sector_codes=[None] * len(bundle.tickers),
        objective=qopt.Objective(lambda_risk=5.0, gamma_concentration=0.03),
        factor_targets=[],
        constraints=qopt.Constraints(long_only=True, weight_min=0.0, weight_max=effective_weight_max),
        risk_free_annual=req.risk_free_annual,
    )
    result = qopt.optimize(inputs)
    warnings.extend(bundle.warnings + result.warnings)
    return AssetOptimizeResponse(
        weights=[
            AssetOptimizeWeight(
                asset_id=asset.asset_id,
                isin=asset.isin or "",
                ticker=asset.ticker,
                name=asset.name,
                weight=float(weight),
            )
            for asset, weight in zip(kept_assets, result.weights)
        ],
        expected_return_annual=float(result.expected_return_annual) if np.isfinite(result.expected_return_annual) else None,
        vol_annual=float(result.vol_annual) if np.isfinite(result.vol_annual) else None,
        sharpe=result.sharpe,
        efficient_frontier=[
            {"lambda": float(p["lambda"]), "ret": float(p["ret"]), "vol": float(p["vol"])}
            for p in result.efficient_frontier
        ],
        diagnostics=result.diagnostics,
        warnings=warnings,
        as_of=str(dates[-1]) if dates else None,
        lookback_start=str(dates[0]) if dates else None,
    )


@router.post("/assets/story", response_model=AssetStoryResponse)
async def asset_story(req: AssetStoryRequest) -> AssetStoryResponse:
    snap = await _build_asset_snapshot(req)
    lang = req.lang
    pct = lambda x: "n/a" if x is None else f"{x * 100:.1f}%"
    beta = snap.summary.beta_vs_benchmark
    risk_bits = [
        f"annualisierte Volatilitaet {pct(snap.summary.vol_annual)}" if lang == "de" else f"annualized volatility {pct(snap.summary.vol_annual)}",
        f"maximaler Drawdown {pct(snap.summary.max_drawdown)} ueber {snap.summary.max_drawdown_days or 0} Tage" if lang == "de" else f"maximum drawdown {pct(snap.summary.max_drawdown)} over {snap.summary.max_drawdown_days or 0} days",
    ]
    if beta is not None:
        risk_bits.append(("Portfolio-Beta " if lang == "de" else "portfolio beta ") + f"{beta:.2f}")
    gaps = snap.warnings + [u.reason for u in snap.unsupported_assets]
    missing_periods = [p.label for p in snap.stress_periods if p.data_status == "unavailable"]
    if lang == "de":
        sections = [
            {
                "title": "Was steckt drin?",
                "body": f"Das Portfolio besteht aus {len(snap.assets)} berechenbaren ETF-Bausteinen. Die groessten Gewichte bestimmen den Lookthrough; bekannte Top-10-Daten decken etwa {pct(snap.lookthrough.known_top10_weight)} des Portfolios ab.",
            },
            {
                "title": "Wichtigste Risiken",
                "body": "Die zentralen Risikosignale sind " + ", ".join(risk_bits) + ". Hohe Ueberschneidungen in denselben Top-Holdings koennen Klumpenrisiken erzeugen.",
            },
            {
                "title": "Stressphasen",
                "body": "2008, 2012, 2020 und 2022 werden separat ausgewertet. Fehlende ETF-Historie wird nicht geschaetzt; sie bleibt sichtbar als Datenluecke" + (f" ({', '.join(missing_periods)})." if missing_periods else "."),
            },
            {
                "title": "Makrobild",
                "body": "Makrooekonomisch reagieren die Bausteine vor allem auf Wachstum, Inflation, Zinsen und Risikoappetit. Das ist eine Orientierung fuer Privatanleger, keine Prognose und keine Kaufempfehlung.",
            },
        ]
        disclaimer = "Keine Anlageberatung. Die Story basiert nur auf verfuegbaren ETF-Preis-, Profil- und Top-10-Holding-Daten."
    else:
        sections = [
            {
                "title": "What is inside?",
                "body": f"The portfolio contains {len(snap.assets)} calculable ETF building blocks. The largest weights drive the look-through; available top-10 data covers about {pct(snap.lookthrough.known_top10_weight)} of the portfolio.",
            },
            {
                "title": "Main risks",
                "body": "The central risk signals are " + ", ".join(risk_bits) + ". Heavy overlap in the same underlying holdings can create concentration risk.",
            },
            {
                "title": "Stress periods",
                "body": "2008, 2012, 2020 and 2022 are evaluated separately. Missing ETF history is not estimated; it remains visible as a data gap" + (f" ({', '.join(missing_periods)})." if missing_periods else "."),
            },
            {
                "title": "Macro view",
                "body": "From a macro angle, the building blocks mainly react to growth, inflation, interest rates and risk appetite. This is plain-language context, not a forecast or recommendation.",
            },
        ]
        disclaimer = "Not investment advice. The story only uses available ETF price, profile and top-10 holding data."
    story = "\n\n".join(f"{section['title']}\n{section['body']}" for section in sections)
    return AssetStoryResponse(story=story, sections=sections, disclaimer=disclaimer, warnings=gaps)


def _price_ticker(t: str, j: str) -> str:
    if j == "JP" and t.upper().endswith(".T"):
        return t[:-2]
    return t


async def _fetch_sector_mu(
    conn,
    us_tickers: list[str],
    jp_tickers: list[str],
    lookback_days: int,
) -> tuple[dict[str, float], dict[str, str | None]]:
    """Return:
    - mu_by_sector: {gics_sector_code: trailing annualized mean return}
      computed from `sec.fact_sector_returns` daily cap-weighted returns.
    - sector_by_ticker: {ticker: gics_sector_code | None} mapping used by the
      caller to assemble the portfolio-level expected return as a weighted
      sum of sector means.

    Tickers are looked up in the per-jurisdiction `dim_company_*` tables. The
    ticker keys mirror the *display form* the caller uses (JP with .T suffix).
    """
    sector_by_ticker: dict[str, str | None] = {}
    if us_tickers:
        rows = await conn.fetch(
            """
            SELECT primary_ticker AS ticker, gics_sector_code
            FROM   dim_company_us
            WHERE  primary_ticker = ANY($1::text[])
            """,
            us_tickers,
        )
        for r in rows:
            sector_by_ticker[r["ticker"]] = (
                str(r["gics_sector_code"]) if r["gics_sector_code"] is not None else None
            )
    if jp_tickers:
        bare = [t[:-2] if t.upper().endswith(".T") else t for t in jp_tickers]
        rows = await conn.fetch(
            """
            SELECT primary_ticker AS ticker, gics_sector_code
            FROM   dim_company_jp
            WHERE  primary_ticker = ANY($1::text[])
            """,
            bare,
        )
        by_bare = {
            r["ticker"]: (str(r["gics_sector_code"]) if r["gics_sector_code"] is not None else None)
            for r in rows
        }
        for display in jp_tickers:
            key = display[:-2] if display.upper().endswith(".T") else display
            sector_by_ticker[display] = by_bare.get(key)

    sector_codes = sorted({s for s in sector_by_ticker.values() if s})
    mu_by_sector: dict[str, float] = {}
    if sector_codes:
        # Pull `lookback_days` trading days of daily cap-weighted sector returns
        # for both jurisdictions; annualize the simple mean.
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT gics_code,
                       cap_weighted_return,
                       ROW_NUMBER() OVER (
                         PARTITION BY gics_code
                         ORDER BY date DESC
                       ) AS rn
                FROM   sec.fact_sector_returns
                WHERE  grouping_level = 'sector'
                  AND  gics_code = ANY($1::text[])
                  AND  cap_weighted_return IS NOT NULL
            )
            SELECT gics_code, AVG(cap_weighted_return) AS mu_d
            FROM   ranked
            WHERE  rn <= $2
            GROUP BY gics_code
            """,
            sector_codes,
            lookback_days,
        )
        for r in rows:
            if r["mu_d"] is None:
                continue
            mu_by_sector[str(r["gics_code"])] = float(r["mu_d"]) * 252.0
    return mu_by_sector, sector_by_ticker


async def _fetch_series(conn, tickers_us: list[str], tickers_jp: list[str], start: date, end: date) -> dict[str, dict[date, float]]:
    """Return {ticker: {date: adj_close}} for the requested tickers, looked up in
    the right per-jurisdiction price table. Ticker keys use the *display* form
    (with .T suffix for JP) so the caller can use them directly."""
    series: dict[str, dict[date, float]] = {}
    if tickers_us:
        rows = await conn.fetch(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM   fact_prices_us
            WHERE  ticker = ANY($1::text[])
              AND  date BETWEEN $2 AND $3
              AND  COALESCE(adj_close, close) IS NOT NULL
            """,
            tickers_us, start, end,
        )
        for r in rows:
            series.setdefault(r["ticker"], {})[r["date"]] = float(r["close"])
    if tickers_jp:
        # JP stored without .T; restore the suffix for the response so the
        # caller's ticker labels match the input.
        bare = [_price_ticker(t, "JP") for t in tickers_jp]
        rows = await conn.fetch(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS close
            FROM   fact_prices_jp
            WHERE  ticker = ANY($1::text[])
              AND  date BETWEEN $2 AND $3
              AND  COALESCE(adj_close, close) IS NOT NULL
            """,
            bare, start, end,
        )
        for r in rows:
            display = r["ticker"] + ".T"
            series.setdefault(display, {})[r["date"]] = float(r["close"])
    return series


def _align(series_map: dict[str, dict[date, float]]) -> tuple[list[date], dict[str, np.ndarray]]:
    """Intersect dates across all series. Returns (dates, {ticker: np.array})."""
    if not series_map:
        return [], {}
    common: set[date] | None = None
    for s in series_map.values():
        common = set(s.keys()) if common is None else (common & set(s.keys()))
    if not common:
        return [], {}
    dates = sorted(common)
    arrays = {t: np.array([series_map[t][d] for d in dates], dtype=float) for t in series_map}
    return dates, arrays


@router.post("/snapshot", response_model=SnapshotResponse)
async def snapshot(req: SnapshotRequest) -> SnapshotResponse:
    warnings: list[str] = []

    holdings = req.holdings
    if not holdings:
        raise HTTPException(status_code=400, detail="No holdings supplied.")

    # Resolve weights. Missing weights → equal-weighted; sum normalized to 1.
    n = len(holdings)
    raw_w = [h.weight if h.weight is not None else 1.0 / n for h in holdings]
    s = sum(raw_w)
    if s <= 0:
        raise HTTPException(status_code=400, detail="Weights sum to zero.")
    if abs(s - 1.0) > 0.005:
        warnings.append(f"Weights summed to {s:.3f}; normalized to 1.0")
    weights = np.array([w / s for w in raw_w], dtype=float)
    tickers = [h.ticker for h in holdings]

    today = date.today()
    start = today - timedelta(days=req.lookback_months * 31 + 14)
    bench = req.benchmark or "SPY"

    us_tickers = [h.ticker for h in holdings if h.jurisdiction == "US"]
    jp_tickers = [h.ticker for h in holdings if h.jurisdiction == "JP"]

    try:
        async with acquire() as conn:
            holdings_series = await _fetch_series(conn, us_tickers, jp_tickers, start, today)
            bench_series = await _fetch_series(conn, [bench], [], start, today)
            sector_mu, sector_by_ticker = await _fetch_sector_mu(
                conn,
                us_tickers,
                jp_tickers,
                lookback_days=252,
            )
    except Exception as exc:
        logger.exception("portfolio snapshot fetch failed")
        raise HTTPException(status_code=500, detail=f"Price fetch failed: {exc}") from exc

    # Drop any holdings with no overlap at all.
    keep_idx = [i for i, t in enumerate(tickers) if holdings_series.get(t)]
    dropped = [tickers[i] for i in range(len(tickers)) if i not in keep_idx]
    if dropped:
        warnings.append(f"Missing price history for {len(dropped)} ticker(s); dropped: {', '.join(dropped[:5])}")
    if not keep_idx:
        return SnapshotResponse(
            summary=SummaryStats(
                expected_return_annual=None, vol_annual=None, sharpe=None,
                max_drawdown_12m=None, max_drawdown_date=None,
                beta_vs_bench=None, corr_vs_bench=None, effective_n=None,
            ),
            equity_curve=[], contributions=[], warnings=warnings,
            as_of=None, lookback_start=None,
        )

    tickers = [tickers[i] for i in keep_idx]
    weights = weights[keep_idx]
    weights = weights / weights.sum()
    kept_series = {t: holdings_series[t] for t in tickers}

    # Intersect dates across kept holdings + benchmark (if present).
    all_series = dict(kept_series)
    if bench_series.get(bench):
        all_series[f"__bench__{bench}"] = bench_series[bench]
    dates, arrays = _align(all_series)

    if len(dates) < 30:
        warnings.append(f"Only {len(dates)} overlapping observations; results may be noisy.")
        if len(dates) < 2:
            return SnapshotResponse(
                summary=SummaryStats(
                    expected_return_annual=None, vol_annual=None, sharpe=None,
                    max_drawdown_12m=None, max_drawdown_date=None,
                    beta_vs_bench=None, corr_vs_bench=None, effective_n=None,
                ),
                equity_curve=[], contributions=[], warnings=warnings,
                as_of=None, lookback_start=str(dates[0]) if dates else None,
            )

    # Build the matrix of holding prices in column order matching `tickers`.
    P = np.column_stack([arrays[t] for t in tickers])
    # Daily simple returns.
    R = P[1:] / P[:-1] - 1.0
    # Daily portfolio return.
    port_ret = R @ weights

    # Stats — annualize with 252.
    mu_d = float(np.nanmean(port_ret))
    sigma_d = float(np.nanstd(port_ret, ddof=1))
    vol_annual = sigma_d * np.sqrt(252.0)

    # Sector-weighted expected 1Y return: map each ticker to its GICS sector,
    # weight that sector's trailing-12M annualized cap-weighted mean by the
    # name's portfolio weight. Falls back to the name's own historical mean
    # when sector lookup fails.
    universe_mu_annual = mu_d * 252.0
    per_name_mu_annual = np.array(
        [float(np.nanmean(R[:, i])) * 252.0 for i in range(R.shape[1])],
        dtype=float,
    )
    fallback_count = 0
    expected_components = np.zeros(R.shape[1], dtype=float)
    for i, t in enumerate(tickers):
        code = sector_by_ticker.get(t)
        mu_i = sector_mu.get(code) if code else None
        if mu_i is None or not np.isfinite(mu_i):
            fallback_count += 1
            mu_i = per_name_mu_annual[i] if np.isfinite(per_name_mu_annual[i]) else universe_mu_annual
        expected_components[i] = mu_i
    exp_ret_annual = float(np.dot(weights, expected_components))
    if fallback_count:
        warnings.append(
            f"Sector mapping missing for {fallback_count} name(s); fell back to per-ticker mean for those weights."
        )

    sharpe = (exp_ret_annual - req.risk_free_annual) / vol_annual if vol_annual > 0 else None

    # NAV curve.
    port_nav = np.concatenate(([1.0], np.cumprod(1.0 + port_ret)))
    # Max drawdown over the trailing-12m window (or the full window if shorter).
    cutoff = max(0, len(port_nav) - 252)
    nav_dd = port_nav[cutoff:]
    if len(nav_dd) > 1:
        peak = np.maximum.accumulate(nav_dd)
        dd = nav_dd / peak - 1.0
        idx = int(np.argmin(dd))
        max_dd = float(dd[idx])
        max_dd_date = str(dates[cutoff + idx])
    else:
        max_dd = None
        max_dd_date = None

    # Benchmark stats.
    bench_key = f"__bench__{bench}"
    if bench_key in arrays:
        B = arrays[bench_key]
        Rb = B[1:] / B[:-1] - 1.0
        # Align — Rb already aligned because we intersected.
        var_b = float(np.var(Rb, ddof=1)) if len(Rb) > 1 else 0.0
        cov = float(np.cov(port_ret, Rb, ddof=1)[0, 1]) if len(Rb) > 1 else 0.0
        beta = cov / var_b if var_b > 0 else None
        corr = float(np.corrcoef(port_ret, Rb)[0, 1]) if len(Rb) > 1 else None
        bench_nav = np.concatenate(([1.0], np.cumprod(1.0 + Rb)))
    else:
        warnings.append(f"Benchmark '{bench}' not found in price tables; skipping beta/corr.")
        beta = None
        corr = None
        bench_nav = np.ones_like(port_nav)

    # Effective N.
    effective_n = float(1.0 / np.sum(weights**2))

    # Per-ticker contributions.
    contributions: list[Contribution] = []
    for i, t in enumerate(tickers):
        ri = R[:, i]
        ri_mu = float(np.nanmean(ri)) * 252.0
        # Contribution to variance = wᵢ × Cov(rᵢ, rₚ).
        cov_ip = float(np.cov(ri, port_ret, ddof=1)[0, 1]) if len(ri) > 1 else 0.0
        vol_contrib = weights[i] * cov_ip * 252.0
        beta_contrib = None
        if bench_key in arrays and var_b > 0:
            cov_ib = float(np.cov(ri, Rb, ddof=1)[0, 1]) if len(ri) > 1 else 0.0
            beta_contrib = weights[i] * (cov_ib / var_b)
        contributions.append(Contribution(
            ticker=t,
            weight=float(weights[i]),
            ret_annual=ri_mu if np.isfinite(ri_mu) else None,
            vol_contrib=vol_contrib if np.isfinite(vol_contrib) else None,
            beta_contrib=beta_contrib if beta_contrib is not None and np.isfinite(beta_contrib) else None,
        ))

    # Curve — downsample to ~120 points to keep payload light.
    step = max(1, len(dates) // 120)
    curve: list[CurvePoint] = []
    for i in range(0, len(dates), step):
        curve.append(CurvePoint(
            date=str(dates[i]),
            nav=float(port_nav[i]),
            bench_nav=float(bench_nav[i]),
        ))
    # Always include last point.
    if curve and curve[-1].date != str(dates[-1]):
        curve.append(CurvePoint(
            date=str(dates[-1]),
            nav=float(port_nav[-1]),
            bench_nav=float(bench_nav[-1]),
        ))

    return SnapshotResponse(
        summary=SummaryStats(
            expected_return_annual=exp_ret_annual if np.isfinite(exp_ret_annual) else None,
            vol_annual=vol_annual if np.isfinite(vol_annual) else None,
            sharpe=sharpe if sharpe is not None and np.isfinite(sharpe) else None,
            max_drawdown_12m=max_dd if max_dd is not None and np.isfinite(max_dd) else None,
            max_drawdown_date=max_dd_date,
            beta_vs_bench=beta if beta is not None and np.isfinite(beta) else None,
            corr_vs_bench=corr if corr is not None and np.isfinite(corr) else None,
            effective_n=effective_n,
        ),
        equity_curve=curve,
        contributions=contributions,
        warnings=warnings,
        as_of=str(dates[-1]),
        lookback_start=str(dates[0]),
    )



# =========================================================================
# /optimize endpoint
# =========================================================================


class OptimizerUniverse(BaseModel):
    tickers: list[str]
    jurisdiction: Literal["US", "JP"] = "US"


class OptimizerObjective(BaseModel):
    lambda_risk: float = 5.0
    gamma_factor: float = 1.0
    gamma_turnover: float = 0.0
    gamma_concentration: float = 0.0


class RiskModelSpec(BaseModel):
    type: Literal["sample", "ledoit_wolf", "ff3", "ff5", "ff5_plus_mom"] = "ledoit_wolf"
    lookback_months: int = Field(default=36, ge=6, le=120)


class FactorTargetSpec(BaseModel):
    factor: str
    mode: Literal["free", "target", "cap", "range"] = "free"
    target: float | None = None
    cap: float | None = None
    lo: float | None = None
    hi: float | None = None
    scale: float = 1.0


class ConstraintsSpec(BaseModel):
    long_only: bool = True
    weight_min: float | None = 0.0
    weight_max: float | None = 1.0
    sector_max: dict[str, float] | None = None
    vol_max_annual: float | None = None
    gross_max: float = 1.0
    current_weights: list[float] | None = None
    # New constraint fields:
    weight_min_per_name: float | None = None
    max_names: int | None = Field(default=None, ge=1, le=500)
    short_max_gross: float | None = Field(default=None, ge=0.0, le=1.0)


class VaRWindowResult(BaseModel):
    """One row of (lookback window × confidence) in the historical VaR table."""
    window_months: int
    alpha: float          # 0.95, 0.99, 0.999
    var: float            # one-day VaR, expressed as a positive fraction of NAV
    cvar: float           # one-day CVaR / Expected Shortfall, positive fraction
    observations: int


class OptimizeRequest(BaseModel):
    universe: OptimizerUniverse
    objective: OptimizerObjective = OptimizerObjective()
    risk_model: RiskModelSpec = RiskModelSpec()
    factor_targets: list[FactorTargetSpec] = Field(default_factory=list)
    constraints: ConstraintsSpec = ConstraintsSpec()
    risk_free_annual: float = 0.045
    # Historical Value-at-Risk grid: each (window_months, alpha) combination
    # is computed against the realized daily return of the solved portfolio.
    var_windows_months: list[int] = Field(default_factory=list)
    var_alphas: list[float] = Field(default_factory=list)


class OptimizeWeight(BaseModel):
    ticker: str
    weight: float


class FrontierPoint(BaseModel):
    lambda_: float = Field(..., alias="lambda")
    ret: float
    vol: float

    model_config = {"populate_by_name": True}


class OptimizeResponse(BaseModel):
    weights: list[OptimizeWeight]
    expected_return_annual: float
    vol_annual: float
    sharpe: float | None
    factor_exposures: dict[str, float]
    marginal_risk_contribution: list[dict]
    efficient_frontier: list[FrontierPoint]
    diagnostics: dict
    warnings: list[str]
    risk_model: RiskModelSpec
    as_of: str | None
    lookback_start: str | None
    historical_var: list[VaRWindowResult] = Field(default_factory=list)


def _compute_historical_var(
    *,
    R: np.ndarray,
    weights: np.ndarray,
    windows_months: list[int],
    alphas: list[float],
) -> list[VaRWindowResult]:
    """Return one VaR / CVaR row per (window, α) pair, computed empirically
    from the realized daily portfolio return Rᵀw over the trailing window.
    VaR is the negative of the α-quantile of the loss distribution, i.e.
    the worst (1-α) fraction. CVaR is the mean of returns below that
    quantile. Both are returned as positive fractions of NAV.
    """
    if R.size == 0 or not windows_months or not alphas:
        return []
    port_ret = R @ weights  # (T,)
    out: list[VaRWindowResult] = []
    for window in windows_months:
        n = int(round(window * 21))  # ≈21 trading days / month
        sample = port_ret[-n:] if n > 0 and len(port_ret) > n else port_ret
        if len(sample) < 10:
            continue
        for alpha in alphas:
            q = float(np.quantile(sample, 1.0 - alpha))
            tail = sample[sample <= q]
            cvar = float(-tail.mean()) if tail.size else float(-q)
            out.append(
                VaRWindowResult(
                    window_months=window,
                    alpha=float(alpha),
                    var=float(-q),
                    cvar=cvar,
                    observations=int(len(sample)),
                )
            )
    return out


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_portfolio(req: OptimizeRequest) -> OptimizeResponse:
    if not req.universe.tickers:
        raise HTTPException(status_code=400, detail="Universe is empty.")

    tickers = [t.upper() for t in req.universe.tickers]
    jurisdiction = req.universe.jurisdiction
    start, end = qrisk.lookback_window(req.risk_model.lookback_months)

    try:
        async with acquire() as conn:
            us_t = tickers if jurisdiction == "US" else []
            jp_t = tickers if jurisdiction == "JP" else []
            price_series = await qrisk.fetch_price_series(conn, us_t, jp_t, start, end)
            kept = [t for t in tickers if price_series.get(t)]
            dropped = [t for t in tickers if t not in kept]
            if not kept:
                raise HTTPException(status_code=503, detail="No price history available for any ticker.")
            if req.constraints.long_only:
                n_kept = len(kept)
                if req.constraints.weight_max is not None and req.constraints.weight_max * n_kept < 1.0 - 1e-9:
                    min_cap = 1.0 / n_kept
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"weight_max={req.constraints.weight_max:.4f} is infeasible for "
                            f"{n_kept} kept tickers; use at least {min_cap:.4f}."
                        ),
                    )
                if req.constraints.weight_min is not None and req.constraints.weight_min * n_kept > 1.0 + 1e-9:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"weight_min={req.constraints.weight_min:.4f} is infeasible for "
                            f"{n_kept} kept tickers."
                        ),
                    )
            dates, arrays = qrisk.align_series({t: price_series[t] for t in kept})
            if len(dates) < 30:
                raise HTTPException(status_code=503, detail=f"Only {len(dates)} overlapping observations; need at least 30.")
            P = np.column_stack([arrays[t] for t in kept])
            R = qrisk.daily_returns_from_levels(P)
            mu_daily = R.mean(axis=0)

            loadings = resid_vol = None
            factor_panel = None
            factor_names = None
            if req.risk_model.type in ("ff3", "ff5", "ff5_plus_mom"):
                ff_loadings, ff_resid = await qrisk.fetch_factor_loadings(
                    conn, kept, jurisdiction, req.risk_model.type, end,
                )
                ff_factor_names = qrisk._FF_FACTORS_BY_MODEL[req.risk_model.type]
                _, panel, _ = await qrisk.fetch_factor_return_panel(
                    conn, ff_factor_names, start, end,
                    jurisdiction=jurisdiction,
                    model=qrisk._FF_MODEL_BY_TYPE.get(req.risk_model.type, "FF5"),
                )
                loadings = ff_loadings
                resid_vol = ff_resid
                factor_panel = panel
                factor_names = ff_factor_names

            sector_codes: list[str | None] = [None] * len(kept)
            if req.constraints.sector_max:
                dim_table = "dim_company_us" if jurisdiction == "US" else "dim_company_jp"
                rows = await conn.fetch(
                    f"SELECT primary_ticker AS ticker, gics_sector_code FROM {dim_table} WHERE primary_ticker = ANY($1::text[])",
                    kept,
                )
                lookup = {r["ticker"]: r["gics_sector_code"] for r in rows}
                sector_codes = [lookup.get(t) for t in kept]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("portfolio optimize fetch failed")
        raise HTTPException(status_code=500, detail=f"Optimize fetch failed: {exc}") from exc

    bundle = qrisk.build_risk_bundle(
        tickers=kept,
        R=R,
        mu_daily=mu_daily,
        model=req.risk_model.type,
        loadings=loadings,
        residual_vol_daily=resid_vol,
        factor_panel=factor_panel,
        factor_names=factor_names,
    )

    factor_targets = [
        qopt.FactorTarget(factor=t.factor, mode=t.mode, target=t.target, cap=t.cap, lo=t.lo, hi=t.hi, scale=t.scale)
        for t in req.factor_targets
    ]
    inputs = qopt.OptimizeRequestInputs(
        tickers=kept,
        mu=bundle.mu,
        sigma=bundle.sigma,
        B=bundle.B,
        factor_names=bundle.factor_names,
        sector_codes=sector_codes,
        objective=qopt.Objective(
            lambda_risk=req.objective.lambda_risk,
            gamma_factor=req.objective.gamma_factor,
            gamma_turnover=req.objective.gamma_turnover,
            gamma_concentration=req.objective.gamma_concentration,
        ),
        factor_targets=factor_targets,
        constraints=qopt.Constraints(
            long_only=req.constraints.long_only,
            weight_min=req.constraints.weight_min,
            weight_max=req.constraints.weight_max,
            gross_max=req.constraints.gross_max,
            sector_max=req.constraints.sector_max,
            vol_max_annual=req.constraints.vol_max_annual,
            current_weights=req.constraints.current_weights,
            weight_min_per_name=req.constraints.weight_min_per_name,
            max_names=req.constraints.max_names,
            short_max_gross=req.constraints.short_max_gross,
        ),
        risk_free_annual=req.risk_free_annual,
    )

    try:
        result = qopt.optimize(inputs)
    except qopt.MipSolverUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    warnings = bundle.warnings + result.warnings
    if dropped:
        warnings.append(f"Dropped tickers with no price history: {', '.join(dropped[:5])}")

    # Historical VaR / CVaR on the realized daily portfolio return.
    historical_var = _compute_historical_var(
        R=R,
        weights=result.weights,
        windows_months=req.var_windows_months,
        alphas=req.var_alphas,
    )

    weights_out = [OptimizeWeight(ticker=t, weight=float(w)) for t, w in zip(kept, result.weights)]
    mc_out = [
        {"ticker": t, "marginal_risk": float(m)}
        for t, m in zip(kept, result.marginal_risk_contribution)
    ]
    frontier_out = [
        FrontierPoint(**{"lambda": p["lambda"], "ret": p["ret"], "vol": p["vol"]})
        for p in result.efficient_frontier
    ]

    return OptimizeResponse(
        weights=weights_out,
        expected_return_annual=float(result.expected_return_annual),
        vol_annual=float(result.vol_annual),
        sharpe=result.sharpe,
        factor_exposures=result.factor_exposures,
        marginal_risk_contribution=mc_out,
        efficient_frontier=frontier_out,
        diagnostics=result.diagnostics,
        warnings=warnings,
        risk_model=req.risk_model,
        as_of=str(dates[-1]) if dates else None,
        lookback_start=str(dates[0]) if dates else None,
        historical_var=historical_var,
    )
