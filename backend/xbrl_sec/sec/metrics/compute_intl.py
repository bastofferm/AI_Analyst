"""Compute the international (Yahoo-backed) metric set, on an FY and a TTM basis.

Reads `fact_yahoo_statement_item` (annual + quarterly statement rows, raw yfinance labels)
and `fact_yahoo_fundamental_metric` (Yahoo profile scalars: trailingPE, priceToBook,
dividendYield, marginals, revenueGrowth, sharesOutstanding, marketCap), reduces
statement labels to canonical line-item names via `_YF_LINE_ITEM_ALIASES`, and
writes rows to `fact_metrics_intl` for the `_SCREENER_METRICS` ids: the 9 the
screener filters on, plus profitability/solvency/growth metrics derived from the
same aliased line items (no extra Yahoo calls). Ids match `ref_metric_definitions`
so INTL shares the SEC/EDINET metric namespace — but note INTL remains a thin
subset of that 169-metric registry, since Yahoo exposes no XBRL line-item detail
and no source-concept trace (`trace_quality='computed_only'`). In parallel
writes an FX-converted market cap into `fact_market_metrics` with
`jurisdiction='INTL'` so the screener's `market_cap_usd` join works.

Bases written (both keyed by the PK's `fiscal_period`, so they coexist):
- 'FY'  — every metric_id, from the latest annual statements + profile scalars.
- 'TTM' — the `_TTM_METRICS` subset, from the newest four contiguous quarters
  (flows summed, stocks taken at the newest quarter). Emitted only where it adds
  information over FY; every guard in `_ttm_window` fails CLOSED, so a doubtful
  window yields no TTM row and the screener transparently falls back to FY.

Design rules:
- Prefer Yahoo profile passthrough for pe/pb/gross_margin/operating_margin/
  dividend_yield/rev_yoy — these are already computed by Yahoo with the fresh
  stock price and are more reliable than reconstructing them.
- Compute rev_cagr_3y, ev_ebitda, fcf_yield from statement line items (Yahoo
  does not expose these directly).
- Skip a company gracefully if a required input is missing — the row is simply
  not written. The screener treats NULL as "does not pass any predicate on this
  key" (`m.<col> IS NOT NULL` in screener.py).
- `trace_quality` for the sibling recon rows is always 'computed_only' — there
  is no XBRL source-concept trace to record.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger(__name__)


# ------------------------------- yfinance alias table (line_item -> raw labels)
# Kept in sync with backend/ai_analyst/services.py::_YF_LINE_ITEM_ALIASES.
# Duplicated (not imported) to avoid an xbrl_sec -> ai_analyst import cycle.
_YF_LINE_ITEM_ALIASES: dict[str, set[str]] = {
    "revenue": {
        "total_revenue", "totalRevenue", "revenue", "sales",
        "operating_revenue", "operatingRevenue", "Total Revenue",
    },
    "gross_profit": {"gross_profit", "grossProfit", "Gross Profit"},
    "earnings_before_interest_taxes_depreciation_amortization": {
        "ebitda", "normalized_ebitda", "normalizedEBITDA",
        "EBITDA", "Normalized EBITDA",
    },
    "earnings_before_interest_taxes": {
        "ebit", "operating_income", "operatingIncome", "operating_profit",
        "income_from_operations", "Operating Income", "EBIT",
    },
    "net_income": {
        "net_income", "netIncome", "net_income_common_stockholders",
        "netIncomeCommonStockholders", "Net Income",
        "Net Income Continuous Operations", "Net Income From Continuing Operations",
    },
    "cash_flow_from_operations": {
        "operating_cash_flow", "operatingCashFlow",
        "total_cash_from_operating_activities",
        "cash_flow_from_continuing_operating_activities",
        "Operating Cash Flow",
    },
    "capital_expenditures": {
        "capital_expenditure", "capitalExpenditure", "capital_expenditures",
        "capital_expenditures_reported", "Capital Expenditure",
    },
    "free_cash_flow": {
        "free_cash_flow", "freeCashFlow", "Free Cash Flow",
    },
    "cash_and_cash_equivalents": {
        "cash_and_cash_equivalents", "cashAndCashEquivalents",
        "cash_cash_equivalents_and_short_term_investments",
        "cashCashEquivalentsAndShortTermInvestments",
        "Cash And Cash Equivalents",
    },
    "total_assets": {"total_assets", "totalAssets", "Total Assets"},
    "total_financial_debt": {
        "total_debt", "totalDebt", "long_term_debt_and_capital_lease_obligation",
        "longTermDebtAndCapitalLeaseObligation", "Total Debt", "Long Term Debt",
    },
    "net_debt": {"net_debt", "netDebt", "Net Debt"},
    "total_equity": {
        "stockholders_equity", "stockholdersEquity", "total_stockholder_equity",
        "totalStockholderEquity", "total_equity_gross_minority_interest",
        "Stockholders Equity", "Total Equity Gross Minority Interest",
    },
}


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


_YF_ALIAS_LOOKUP: dict[str, str] = {
    _norm(alias): line_item
    for line_item, aliases in _YF_LINE_ITEM_ALIASES.items()
    for alias in {*aliases, line_item}
}


# ---------------- flow vs stock semantics (required for TTM aggregation)
# There is no registry for this: ref_metric_definitions.metric_type is the literal
# 'FNDM' for every row and carries no period semantics. So the classification is
# declared here, and the assertion below IS the registry — it is the only thing
# stopping a future 14th alias from being silently unclassified and silently
# dropped from every TTM window.
#
# FLOW  = accrues over the period  -> summed across the 4 quarters of a TTM window.
# STOCK = balance at a point in time -> taken from the newest quarter, never summed.
_FLOW_LINE_ITEMS: frozenset[str] = frozenset({
    "revenue",
    "gross_profit",
    "earnings_before_interest_taxes_depreciation_amortization",
    "earnings_before_interest_taxes",
    "net_income",
    "cash_flow_from_operations",
    "capital_expenditures",
    "free_cash_flow",
})
_STOCK_LINE_ITEMS: frozenset[str] = frozenset({
    "cash_and_cash_equivalents",
    "total_assets",
    "total_financial_debt",
    "net_debt",
    "total_equity",
})

assert _FLOW_LINE_ITEMS | _STOCK_LINE_ITEMS == set(_YF_LINE_ITEM_ALIASES), (
    "every canonical line item must be classified flow or stock for TTM; unclassified: "
    f"{set(_YF_LINE_ITEM_ALIASES) - (_FLOW_LINE_ITEMS | _STOCK_LINE_ITEMS)}"
)

# A TTM window must span four consecutive quarters. Real quarters run 89-92 days
# (53-week calendars reach ~98). Guarding each gap — rather than the total span —
# is what rejects a window with a missing quarter: three gaps of a correct window
# total ~273 days, so a one-quarter-gap window spans ~365 and would slip past a
# `span > 400` rule while being flatly wrong. It also rejects semi-annual filers
# (~182-day gaps), which is the majority of HKEX.
_QUARTER_GAP_MIN_DAYS = 60
_QUARTER_GAP_MAX_DAYS = 120


# Defensive fallback: infer the *trading* currency from the ticker suffix. Yahoo
# discovery sometimes writes financialCurrency (e.g. 'USD' for an Indonesian miner)
# into dim_company_intl.currency instead of Yahoo's `.info['currency']` (IDR). The
# suffix→ccy table below overrides the stored value for known exchanges.
_SUFFIX_CURRENCY: dict[str, str] = {
    ".JK": "IDR",   ".KS": "KRW",   ".KQ": "KRW",   ".SS": "CNY",   ".SZ": "CNY",
    ".HK": "HKD",   ".T":  "JPY",   ".TYO": "JPY",  ".NS": "INR",   ".BO": "INR",
    ".BK": "THB",   ".KL": "MYR",   ".SI": "SGD",   ".TW": "TWD",   ".TWO": "TWD",
    ".SA": "BRL",   ".MX": "MXN",   ".JO": "ZAR",   ".BA": "ARS",   ".SN": "CLP",
    ".TA": "ILS",   ".IS": "TRY",   ".WA": "PLN",   ".PR": "CZK",   ".BD": "HUF",
    ".AS": "EUR",   ".DE": "EUR",   ".F":  "EUR",   ".MI": "EUR",   ".MC": "EUR",
    ".PA": "EUR",   ".BR": "EUR",   ".LS": "EUR",   ".VI": "EUR",   ".HE": "EUR",
    ".ST": "SEK",   ".OL": "NOK",   ".CO": "DKK",   ".BE": "EUR",
    ".L":  "GBp",   ".IL": "GBp",   ".SW": "CHF",   ".VX": "CHF",
    ".TO": "CAD",   ".V":  "CAD",   ".CN": "CAD",   ".NE": "CAD",
    ".AX": "AUD",   ".NZ": "NZD",   ".DU": "AED",
}


def _infer_currency(ticker: str, stored_ccy: str | None) -> str:
    """Prefer the ticker suffix over the stored currency to work around Yahoo
    discovery bugs. GBp (British pence, 100 pence = 1 GBP) is preserved as a
    separate ccy since market caps come back in pence."""
    if ticker:
        for suffix, ccy in _SUFFIX_CURRENCY.items():
            if ticker.endswith(suffix):
                return ccy
    return (stored_ccy or "USD").upper()


# ---------------- Screener metric_ids we materialize + their category/unit
_SCREENER_METRICS: list[dict[str, Any]] = [
    {"metric_id": "price_to_earnings_trailing",             "category": "valuation",      "unit_type": "ratio"},
    {"metric_id": "price_to_book",                          "category": "valuation",      "unit_type": "ratio"},
    {"metric_id": "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
                                                            "category": "valuation",      "unit_type": "ratio"},
    {"metric_id": "free_cash_flow_yield",                   "category": "valuation",      "unit_type": "pct"},
    {"metric_id": "dividend_yield",                         "category": "shareholder",    "unit_type": "pct"},
    {"metric_id": "gross_margin",                           "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "operating_margin",                       "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "revenue_growth_year_over_year",          "category": "growth",         "unit_type": "pct"},
    {"metric_id": "revenue_compound_annual_growth_rate_3_year",
                                                            "category": "growth",         "unit_type": "pct"},
    # -- Derived from the same 13 aliased line items (no extra Yahoo calls). metric_ids
    #    are the canonical ones from ref_metric_definitions so INTL rows line up with
    #    the SEC/EDINET pipelines' metric namespace.
    {"metric_id": "net_profit_margin",                      "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "earnings_before_interest_taxes_depreciation_amortization_margin",
                                                            "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "return_on_equity",                       "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "return_on_assets",                       "category": "profitability",  "unit_type": "pct"},
    {"metric_id": "asset_turnover",                         "category": "profitability",  "unit_type": "ratio"},
    {"metric_id": "total_financial_debt_to_equity",         "category": "solvency_and_liquidity", "unit_type": "ratio"},
    {"metric_id": "net_debt_to_earnings_before_interest_taxes_depreciation_amortization",
                                                            "category": "solvency_and_liquidity", "unit_type": "ratio"},
    {"metric_id": "price_to_free_cash_flow",                "category": "valuation",      "unit_type": "ratio"},
    {"metric_id": "free_cash_flow_growth_year_over_year",   "category": "growth",         "unit_type": "pct"},
    {"metric_id": "earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year",
                                                            "category": "growth",         "unit_type": "pct"},
]

# ---------------- which metric_ids get a second, TTM-basis row
# Emitted only where TTM adds information over the FY row. Excluded, and why:
#   price_to_earnings_trailing -- Yahoo's trailingPE is ALREADY TTM; a TTM row
#       would be a byte-identical duplicate.
#   price_to_book / dividend_yield -- already live price / MRQ book / current yield.
#   total_financial_debt_to_equity -- both inputs are stocks; a "trailing-twelve-month"
#       balance-sheet ratio is a category error. (The honest basis for that is MRQ.)
#   the growth/CAGR ids -- need 8 (YoY) to ~16 (3y CAGR) contiguous quarters, but
#       yfinance serves only 4-5 per frame, so they would be None for ~everyone.
# The three margins are handled conditionally in _compute_one: TTM only when Yahoo's
# own (already-TTM) profile scalar is absent, so we never overwrite Yahoo's clean
# number with our alias reconstruction.
_TTM_METRICS: frozenset[str] = frozenset({
    "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization",
    "free_cash_flow_yield",
    "price_to_free_cash_flow",
    "earnings_before_interest_taxes_depreciation_amortization_margin",
    "return_on_equity",
    "return_on_assets",
    "asset_turnover",
    "net_debt_to_earnings_before_interest_taxes_depreciation_amortization",
    "gross_margin",
    "operating_margin",
    "net_profit_margin",
})

# metric_id -> the Yahoo profile scalar that supersedes our statement reconstruction.
# These profile values are already TTM and are Yahoo's own clean computation.
_MARGIN_PROFILE_KEYS: dict[str, str] = {
    "gross_margin": "grossMargins",
    "operating_margin": "operatingMargins",
    "net_profit_margin": "profitMargins",
}

# Yahoo profile metric_ids (fact_yahoo_fundamental_metric) we consume directly.
_PROFILE_KEYS = (
    "trailingPE", "priceToBook", "dividendYield",
    "profitMargins", "grossMargins", "operatingMargins",
    "revenueGrowth", "sharesOutstanding", "marketCap",
    "enterpriseValue", "trailingEps",
)


# ------------------------------- data loaders

def _load_universe(cur, only_intl_company_ids: list[str] | None) -> list[dict[str, Any]]:
    where = ["primary_ticker IS NOT NULL", "COALESCE(include_in_pipeline, true)"]
    params: list[Any] = []
    if only_intl_company_ids:
        where.append("intl_company_id = ANY(%s)")
        params.append([str(v) for v in only_intl_company_ids])
    cur.execute(
        f"""
        SELECT intl_company_id, primary_ticker, currency, market_cap, shares_outstanding
        FROM   dim_company_intl
        WHERE  {' AND '.join(where)}
        ORDER  BY intl_company_id
        """,
        params,
    )
    return [
        {
            "intl_company_id": str(row[0]),
            "ticker": str(row[1]),
            "currency": (row[2] or "USD"),
            "market_cap_local": float(row[3]) if row[3] is not None else None,
            "shares_outstanding": float(row[4]) if row[4] is not None else None,
        }
        for row in cur.fetchall()
    ]


def _load_statement_periods(
    cur, intl_company_id: str, period_type: str
) -> tuple[dict[date, dict[str, float]], dict[date, str | None]]:
    """Return ({period_end: {line_item: value}}, {period_end: currency}) for one
    period_type ('annual' | 'quarterly'), keyed by the REAL period_end."""
    cur.execute(
        """
        SELECT period_end, line_item, value, currency
        FROM   fact_yahoo_statement_item
        WHERE  intl_company_id = %s AND period_type = %s AND value IS NOT NULL
        """,
        (intl_company_id, period_type),
    )
    by_period: dict[date, dict[str, float]] = defaultdict(dict)
    ccy_by_period: dict[date, str | None] = {}
    for period_end, label, value, currency in cur.fetchall():
        if not isinstance(period_end, date):
            continue
        canonical = _YF_ALIAS_LOOKUP.get(_norm(str(label)))
        if not canonical:
            continue
        ccy_by_period.setdefault(period_end, currency)
        # Prefer the largest absolute value if duplicates arrive (yfinance sometimes
        # emits both continuing-ops and total variants).
        prev = by_period[period_end].get(canonical)
        if prev is None or abs(float(value)) > abs(prev):
            by_period[period_end][canonical] = float(value)
    return by_period, ccy_by_period


def _load_statements(cur, intl_company_id: str) -> tuple[dict[int, dict[str, float]], dict[int, date]]:
    """Return ({fiscal_year: {line_item: value}}, {fiscal_year: real period_end}).

    The period_end map is what lets `_compute_one` stamp the true fiscal-year end
    instead of a synthesized Dec-31, and it anchors the TTM information-gain guard.
    """
    by_period, _ = _load_statement_periods(cur, intl_company_id, "annual")
    by_year: dict[int, dict[str, float]] = defaultdict(dict)
    end_by_year: dict[int, date] = {}
    for period_end in sorted(by_period):  # ascending: later periods win the end date
        fy = period_end.year
        end_by_year[fy] = max(period_end, end_by_year.get(fy, period_end))
        for canonical, value in by_period[period_end].items():
            prev = by_year[fy].get(canonical)
            if prev is None or abs(value) > abs(prev):
                by_year[fy][canonical] = value
    return by_year, end_by_year


def _load_quarters(cur, intl_company_id: str) -> tuple[dict[date, dict[str, float]], dict[date, str | None]]:
    """Return ({period_end: {line_item: value}}, {period_end: currency}) for quarters."""
    return _load_statement_periods(cur, intl_company_id, "quarterly")


def _load_profile(cur, intl_company_id: str) -> dict[str, float]:
    cur.execute(
        """
        SELECT metric_id, value
        FROM   fact_yahoo_fundamental_metric
        WHERE  intl_company_id = %s AND metric_id = ANY(%s) AND value IS NOT NULL
        """,
        (intl_company_id, list(_PROFILE_KEYS)),
    )
    out: dict[str, float] = {}
    for metric_id, value in cur.fetchall():
        try:
            out[str(metric_id)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _load_fx_map(cur, currencies: Iterable[str]) -> dict[str, float]:
    ccys = sorted({c for c in currencies if c and c != "USD"})
    if not ccys:
        return {"USD": 1.0}
    cur.execute(
        """
        SELECT DISTINCT ON (ccy) ccy, usd_per_unit
        FROM   fact_fx
        WHERE  ccy = ANY(%s)
        ORDER  BY ccy, fx_date DESC
        """,
        (ccys,),
    )
    fx: dict[str, float] = {"USD": 1.0}
    for ccy, rate in cur.fetchall():
        try:
            fx[str(ccy)] = float(rate)
        except (TypeError, ValueError):
            continue
    return fx


# ------------------------------- metric computation

def _pct(value: Any) -> float | None:
    """Yahoo margins/growth come in as decimals (0.32). The screener's filter
    catalogue also uses decimal semantics (fcf_yield=0.06, rev_yoy=0.15), so we
    pass through. Return None if not a finite number."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _ratio(numerator: Any, denominator: Any) -> float | None:
    try:
        n = float(numerator)
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d == 0 or d != d:
        return None
    return n / d


def _yahoo_percent_to_decimal(value: Any) -> float | None:
    """Yahoo's .info['dividendYield'] is a PERCENT number (3.44 == 3.44%), unlike
    its margins/growth fields which are already decimals (0.32 == 32%). The screener
    catalogue and the SEC/EDINET pipelines both use decimal semantics, so normalize
    it here — otherwise INTL dividend yields land 100x too high."""
    f = _pct(value)
    return None if f is None else f / 100.0


def _ratio_pos_denom(numerator: Any, denominator: Any) -> float | None:
    """Like _ratio but requires a strictly positive denominator. Used for ROE and
    debt/equity: a negative book value makes the ratio sign-flip into nonsense
    (a loss-making firm with negative equity would score a 'good' positive ROE)."""
    try:
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return _ratio(numerator, d)


def _growth(new_value: Any, old_value: Any) -> float | None:
    """YoY growth. Returns None off a non-positive base — growth from a negative
    or zero prior value is not interpretable."""
    try:
        n = float(new_value)
        o = float(old_value)
    except (TypeError, ValueError):
        return None
    if o <= 0 or o != o or n != n:
        return None
    return n / o - 1.0


def _prefer(primary: float | None, fallback: float | None) -> float | None:
    """Prefer `primary` when it is a real number — including 0.0. A truthy `or` would
    silently discard a genuine zero margin/growth and fall through to the fallback."""
    return primary if primary is not None else fallback


def _derive_shared(
    *,
    rev: float | None,
    gross: float | None,
    ebit: float | None,
    ebitda: float | None,
    net_income: float | None,
    fcf: float | None,
    total_assets: float | None,
    total_equity: float | None,
    total_debt: float | None,
    net_debt: float | None,
    mcap_native: float | None,
    ev_native: float | None,
) -> dict[str, float | None]:
    """The metric arithmetic that is identical on the FY and TTM bases.

    Shared deliberately: there must be exactly one definition of net_debt/EBITDA et al.
    Two copies would drift. The caller decides WHICH period's flows and stocks to pass;
    this function only knows the formulas.
    """
    return {
        "enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization":
            _ratio(ev_native, ebitda),
        "free_cash_flow_yield": _ratio(fcf, mcap_native),
        "price_to_free_cash_flow": _ratio_pos_denom(mcap_native, fcf),
        "earnings_before_interest_taxes_depreciation_amortization_margin": _ratio(ebitda, rev),
        "return_on_equity": _ratio_pos_denom(net_income, total_equity),
        "return_on_assets": _ratio_pos_denom(net_income, total_assets),
        "asset_turnover": _ratio_pos_denom(rev, total_assets),
        "total_financial_debt_to_equity": _ratio_pos_denom(total_debt, total_equity),
        "net_debt_to_earnings_before_interest_taxes_depreciation_amortization":
            _ratio_pos_denom(net_debt, ebitda),
        "gross_margin": _ratio(gross, rev),
        "operating_margin": _ratio(ebit, rev),
        "net_profit_margin": _ratio(net_income, rev),
    }


_IMPORTANCE_MAP: dict[str, int] | None = None


def _importance_for(metric_id: str) -> int | None:
    """Registry importance for a metric_id (cached). Lets the cross-sectional feature
    selection (`ic._load_metric_ids`, importance<=2) pick INTL features exactly as US/JP —
    importance is a global per-metric property, so it comes straight from the registry."""
    global _IMPORTANCE_MAP
    if _IMPORTANCE_MAP is None:
        try:
            from xbrl_sec.sec.db.connection import connect
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT metric_id, importance FROM ref_metric_definitions")
                _IMPORTANCE_MAP = {str(m): i for m, i in cur.fetchall()}
        except Exception:  # noqa: BLE001 - degrade to null importance if the registry is unreadable
            _IMPORTANCE_MAP = {}
    return _IMPORTANCE_MAP.get(metric_id)


def _row(
    ticker: str,
    intl_id: str,
    fiscal_year: int,
    fiscal_period: str,
    period_end: date | None,
    metric_id: str,
    value: float,
    meta: dict[str, Any],
) -> tuple:
    return (
        ticker,
        intl_id,
        None,                        # primary_id
        None,                        # primary_id_type
        "INTL",                      # jurisdiction
        int(fiscal_year),
        fiscal_period,
        period_end,
        metric_id,
        None,                        # formula (kept null for INTL; source is Yahoo/derived)
        "derived",                   # metric_type
        meta["category"],
        _importance_for(metric_id),  # importance (from ref_metric_definitions; enables features)
        meta["unit_type"],
        float(value),
        None,                        # currency (ratios/pcts)
        False,                       # fallback_applied
    )


def _ttm_window(
    quarters: dict[date, dict[str, float]],
    latest_annual_end: date | None,
    quarter_currencies: dict[date, str | None] | None = None,
) -> tuple[dict[str, float] | None, date | None]:
    """Fold the newest four quarters into one trailing-twelve-month bundle.

    Returns (bundle, window_end) or (None, None). Fails CLOSED: any doubt about the
    window and we emit nothing, so the caller's FY row stands rather than a wrong TTM
    silently outranking it.

    Flows are summed all-or-nothing; stocks are taken from the newest quarter.
    """
    if len(quarters) < 4:
        return None, None
    ends = sorted(quarters)[-4:]

    # Consecutive-quarter guard (see _QUARTER_GAP_MIN/MAX_DAYS): rejects a window with
    # a missing quarter, and rejects semi-annual filers.
    for older, newer in zip(ends, ends[1:]):
        gap = (newer - older).days
        if not (_QUARTER_GAP_MIN_DAYS <= gap <= _QUARTER_GAP_MAX_DAYS):
            return None, None

    # Yahoo can switch financialCurrency mid-history; summing across a switch is junk.
    if quarter_currencies is not None:
        ccys = {quarter_currencies.get(e) for e in ends}
        if len(ccys) > 1:
            return None, None

    # Information-gain guard. Without this, a company that STOPPED reporting still has
    # four internally-contiguous (but stale) quarters that would pass every date check
    # above and then outrank its fresher FY row — a strict regression. It also drops the
    # degenerate case where the window ends on the fiscal-year end, i.e. TTM == FY.
    if latest_annual_end is not None and ends[-1] <= latest_annual_end:
        return None, None

    bundle: dict[str, float] = {}
    for item in _FLOW_LINE_ITEMS:
        vals = [quarters[e].get(item) for e in ends]
        if all(v is not None for v in vals):
            bundle[item] = float(sum(vals))  # type: ignore[arg-type]
    for item in _STOCK_LINE_ITEMS:
        v = quarters[ends[-1]].get(item)
        if v is not None:
            bundle[item] = float(v)

    # Revenue anchors every margin/turnover ratio; without it the window is not worth
    # emitting (matches committee/quarterly.py's behaviour).
    if bundle.get("revenue") is None:
        return None, None
    return bundle, ends[-1]


def _cagr(new_value: Any, old_value: Any, years: int) -> float | None:
    try:
        n = float(new_value)
        o = float(old_value)
    except (TypeError, ValueError):
        return None
    if o <= 0 or n <= 0 or years <= 0:
        return None
    return (n / o) ** (1.0 / years) - 1.0


def _compute_one(
    company: dict[str, Any],
    statements: dict[int, dict[str, float]],
    profile: dict[str, float],
    fx_map: dict[str, float] | None = None,
    *,
    annual_ends: dict[int, date] | None = None,
    quarters: dict[date, dict[str, float]] | None = None,
    quarter_currencies: dict[date, str | None] | None = None,
) -> tuple[list[tuple], date | None, float | None]:
    """Return (metric-rows, latest-annual-period-end, USD market cap for fact_market_metrics).

    Emits FY rows always, plus TTM rows for the `_TTM_METRICS` subset when `quarters`
    contains a clean four-quarter window (see `_ttm_window`). The returned period_end
    stays the ANNUAL one — the fact_market_metrics sidecar is keyed off it.

    annual_ends: {fiscal_year: real period_end} from `_load_statements`.
    quarters / quarter_currencies: from `_load_quarters`. Omit both to compute FY only.

    fx_map: {ccy: usd_per_unit} from fact_fx. Used to convert local-currency
    market cap into USD for fact_market_metrics. Yahoo's `.info['marketCap']`
    is in the trading currency, NOT USD-normalized, so this conversion is required.
    """
    if not statements:
        return [], None, None
    ticker = company["ticker"]
    intl_id = company["intl_company_id"]
    latest_fy = max(statements)
    prev_fy = max((y for y in statements if y < latest_fy), default=None)
    three_ago = max((y for y in statements if y <= latest_fy - 3), default=None)
    latest = statements[latest_fy]
    # The real fiscal-year end. The old `date(latest_fy, 12, 31)` was wrong for every
    # non-December filer (SIE.DE's FY ends 30 Sep and was stamped three months into the
    # future), and a period_end-windowed TTM cannot be anchored on a fabricated date.
    period_end = (annual_ends or {}).get(latest_fy) or date(latest_fy, 12, 31)

    rev = latest.get("revenue")
    ebit = latest.get("earnings_before_interest_taxes")
    ebitda = latest.get("earnings_before_interest_taxes_depreciation_amortization")
    fcf = latest.get("free_cash_flow")
    gross = latest.get("gross_profit")
    total_debt = latest.get("total_financial_debt")
    cash = latest.get("cash_and_cash_equivalents")
    net_income = latest.get("net_income")
    total_assets = latest.get("total_assets")
    total_equity = latest.get("total_equity")
    # Yahoo only sometimes emits an explicit "Net Debt"; reconstruct when absent.
    net_debt = latest.get("net_debt")
    if net_debt is None and total_debt is not None:
        net_debt = total_debt - (cash or 0.0)
    prev_stmt = statements.get(prev_fy, {}) if prev_fy else {}
    prev_rev = prev_stmt.get("revenue")
    prev_fcf = prev_stmt.get("free_cash_flow")
    prev_ebitda = prev_stmt.get("earnings_before_interest_taxes_depreciation_amortization")
    three_rev = statements.get(three_ago, {}).get("revenue") if three_ago else None

    # Market cap: Yahoo's profile.marketCap and dim_company_intl.market_cap are BOTH
    # in the ticker's trading currency (native), never USD-normalized. Prefer profile
    # marketCap when present (freshest), else fall back to the stored dim value.
    mcap_native = profile.get("marketCap") or company.get("market_cap_local")
    # Enterprise value: profile.enterpriseValue if present, else compute (same native ccy).
    ev_native = profile.get("enterpriseValue")
    if ev_native is None and mcap_native is not None:
        ev_native = mcap_native + (total_debt or 0) - (cash or 0)

    metrics: dict[str, float | None] = _derive_shared(
        rev=rev, gross=gross, ebit=ebit, ebitda=ebitda, net_income=net_income, fcf=fcf,
        total_assets=total_assets, total_equity=total_equity, total_debt=total_debt,
        net_debt=net_debt, mcap_native=mcap_native, ev_native=ev_native,
    )
    metrics.update({
        "price_to_earnings_trailing":   _pct(profile.get("trailingPE")),
        "price_to_book":                _pct(profile.get("priceToBook")),
        "dividend_yield":               _yahoo_percent_to_decimal(profile.get("dividendYield")),
        "revenue_growth_year_over_year":
            _prefer(_pct(profile.get("revenueGrowth")), _growth(rev, prev_rev)),
        "revenue_compound_annual_growth_rate_3_year": _cagr(rev, three_rev, 3),
        "free_cash_flow_growth_year_over_year": _growth(fcf, prev_fcf),
        "earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year":
            _growth(ebitda, prev_ebitda),
    })
    # Yahoo's own margins are already TTM and cleaner than our alias reconstruction, so
    # they win when present. Tested with `is not None`, never a truthy `or` — a genuine
    # 0.0 margin is a real value, not a miss.
    for metric_id, profile_key in _MARGIN_PROFILE_KEYS.items():
        profile_value = _pct(profile.get(profile_key))
        if profile_value is not None:
            metrics[metric_id] = profile_value

    metric_meta = {m["metric_id"]: m for m in _SCREENER_METRICS}
    rows: list[tuple] = [
        _row(ticker, intl_id, latest_fy, "FY", period_end, metric_id, value, metric_meta[metric_id])
        for metric_id, value in metrics.items()
        if value is not None
    ]

    # ---------------- historical FY rows (prior years' statement-derived metrics)
    # The cross-sectional alpha model needs multiple annual cross-sections; with only the
    # latest FY it sees ~1 date and cannot train per country. Yahoo statements already carry
    # 4-5 years, so emit prior-year FY rows for the STATEMENT-DERIVED metrics (margins, ROE,
    # ROA, leverage, growth). Price/valuation metrics (P/E, EV/EBITDA, FCF yield, div yield)
    # are omitted for prior years on purpose: the profile snapshot only has TODAY's price, and
    # pairing it with an old fiscal year would be wrong. Passing mcap/ev=None drops them.
    for fy in sorted(statements):
        if fy >= latest_fy:
            continue
        y = statements[fy]
        p_fy = max((yr for yr in statements if yr < fy), default=None)
        t_fy = max((yr for yr in statements if yr <= fy - 3), default=None)
        p = statements.get(p_fy, {}) if p_fy is not None else {}
        y_rev = y.get("revenue")
        y_ebitda = y.get("earnings_before_interest_taxes_depreciation_amortization")
        y_debt = y.get("total_financial_debt")
        y_net_debt = y.get("net_debt")
        if y_net_debt is None and y_debt is not None:
            y_net_debt = y_debt - (y.get("cash_and_cash_equivalents") or 0.0)
        y_metrics = _derive_shared(
            rev=y_rev, gross=y.get("gross_profit"), ebit=y.get("earnings_before_interest_taxes"),
            ebitda=y_ebitda, net_income=y.get("net_income"), fcf=y.get("free_cash_flow"),
            total_assets=y.get("total_assets"), total_equity=y.get("total_equity"),
            total_debt=y_debt, net_debt=y_net_debt, mcap_native=None, ev_native=None,
        )
        y_metrics["revenue_growth_year_over_year"] = _growth(y_rev, p.get("revenue"))
        y_metrics["revenue_compound_annual_growth_rate_3_year"] = _cagr(
            y_rev, statements.get(t_fy, {}).get("revenue") if t_fy is not None else None, 3)
        y_metrics["free_cash_flow_growth_year_over_year"] = _growth(
            y.get("free_cash_flow"), p.get("free_cash_flow"))
        y_metrics["earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year"] = _growth(
            y_ebitda, p.get("earnings_before_interest_taxes_depreciation_amortization"))
        y_end = (annual_ends or {}).get(fy) or date(fy, 12, 31)
        rows.extend(
            _row(ticker, intl_id, fy, "FY", y_end, mid, val, metric_meta[mid])
            for mid, val in y_metrics.items()
            if val is not None and mid in metric_meta
        )

    # ---------------- TTM basis (second row per eligible metric_id)
    # The screener resolves per metric_id, so a partial TTM set is by design: each
    # metric independently takes TTM when available and falls back to FY otherwise.
    ttm_bundle, ttm_end = _ttm_window(quarters or {}, period_end, quarter_currencies)
    if ttm_bundle is not None and ttm_end is not None:
        t_debt = ttm_bundle.get("total_financial_debt")
        t_cash = ttm_bundle.get("cash_and_cash_equivalents")
        t_net_debt = ttm_bundle.get("net_debt")
        if t_net_debt is None and t_debt is not None:
            t_net_debt = t_debt - (t_cash or 0.0)
        # EV has to be dated with the same balance sheet as the EBITDA it divides. When
        # Yahoo supplies no enterpriseValue we rebuild it from the LATEST QUARTER's
        # debt/cash — using the annual ones would pair a TTM numerator with a year-old
        # balance sheet.
        t_ev = profile.get("enterpriseValue")
        if t_ev is None and mcap_native is not None:
            t_ev = mcap_native + (t_debt or 0) - (t_cash or 0)
        ttm_metrics = _derive_shared(
            rev=ttm_bundle.get("revenue"),
            gross=ttm_bundle.get("gross_profit"),
            ebit=ttm_bundle.get("earnings_before_interest_taxes"),
            ebitda=ttm_bundle.get("earnings_before_interest_taxes_depreciation_amortization"),
            net_income=ttm_bundle.get("net_income"),
            fcf=ttm_bundle.get("free_cash_flow"),
            total_assets=ttm_bundle.get("total_assets"),
            total_equity=ttm_bundle.get("total_equity"),
            total_debt=t_debt,
            net_debt=t_net_debt,
            mcap_native=mcap_native,
            ev_native=t_ev,
        )
        for metric_id, value in ttm_metrics.items():
            if value is None or metric_id not in _TTM_METRICS:
                continue
            # Where Yahoo gave its own (already-TTM) margin, the FY row already carries
            # the better number — emitting ours would be a strictly worse duplicate.
            profile_key = _MARGIN_PROFILE_KEYS.get(metric_id)
            if profile_key is not None and _pct(profile.get(profile_key)) is not None:
                continue
            rows.append(
                _row(ticker, intl_id, ttm_end.year, "TTM", ttm_end, metric_id, value,
                     metric_meta[metric_id])
            )

    # Convert native market cap → USD via fact_fx. Yahoo's marketCap is in the ticker's
    # trading currency, so KRW/JPY/INR/etc. tickers need explicit conversion. Infer
    # the trading currency from the ticker suffix — the stored `currency` field is
    # unreliable (Yahoo sometimes writes financialCurrency instead).
    mcap_usd: float | None = None
    if mcap_native is not None:
        ccy = _infer_currency(ticker, company.get("currency"))
        if ccy == "GBp":  # British pence: 100 pence = 1 GBP
            rate = (fx_map or {}).get("GBP")
            if rate is not None:
                mcap_usd = float(mcap_native) * float(rate) / 100.0
        else:
            rate = (fx_map or {}).get(ccy)
            if rate is None and ccy == "USD":
                rate = 1.0
            if rate:
                mcap_usd = float(mcap_native) * float(rate)
    return rows, period_end, mcap_usd


# ------------------------------- writes

def _upsert_metrics(cur, rows: list[tuple]) -> int:
    if not rows:
        return 0
    return execute_values(
        cur,
        """
        INSERT INTO fact_metrics_intl
            (ticker, intl_company_id, primary_id, primary_id_type, jurisdiction,
             fiscal_year, fiscal_period, period_end, metric_id, formula, metric_type,
             category, importance, unit_type, value, currency, fallback_applied)
        VALUES %s
        ON CONFLICT (ticker, intl_company_id, fiscal_year, fiscal_period, metric_id)
        DO UPDATE SET
            value            = EXCLUDED.value,
            unit_type        = EXCLUDED.unit_type,
            category         = EXCLUDED.category,
            metric_type      = EXCLUDED.metric_type,
            period_end       = EXCLUDED.period_end,
            fallback_applied = EXCLUDED.fallback_applied,
            computed_at      = now()
        """,
        rows,
        page_size=500,
    )


def _delete_ttm(cur, intl_company_ids: list[str]) -> int:
    """Drop this run's existing TTM rows before re-inserting.

    Required for the fail-closed guards to actually bite on a re-run: a company that
    LOSES TTM eligibility (stops reporting quarterly, develops a gap, switches currency)
    must lose its TTM rows, otherwise a stale TTM keeps outranking the fresher FY row
    forever. FY rows keep plain upsert semantics — they are legitimate history.
    Scoped to the processed universe so --ids/--limit runs stay safe.
    """
    if not intl_company_ids:
        return 0
    cur.execute(
        "DELETE FROM fact_metrics_intl WHERE intl_company_id = ANY(%s) AND fiscal_period = 'TTM'",
        (intl_company_ids,),
    )
    return cur.rowcount or 0


def _upsert_market_cap(
    cur,
    mcap_rows: list[tuple[str, str, float, date | None]],
) -> int:
    """Write market_capitalization to fact_market_metrics for INTL entities."""
    if not mcap_rows:
        return 0
    payload = [
        (
            "INTL", intl_id, ticker, int(period_end.year) if period_end else 1900,
            "FY", period_end, period_end, "market_capitalization",
            float(usd), "USD", "yahoo_intl",
        )
        for ticker, intl_id, usd, period_end in mcap_rows
    ]
    return execute_values(
        cur,
        """
        INSERT INTO fact_market_metrics
            (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period,
             period_end, market_date, metric_id, value, currency, source)
        VALUES %s
        ON CONFLICT (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, metric_id)
        DO UPDATE SET
            value       = EXCLUDED.value,
            currency    = EXCLUDED.currency,
            market_date = EXCLUDED.market_date,
            source      = EXCLUDED.source,
            updated_at  = now()
        """,
        payload,
        page_size=500,
    )


# ------------------------------- entrypoint

def compute_intl_metrics(
    *,
    limit: int | None = None,
    only_intl_company_ids: list[str] | None = None,
    verbose: bool = False,
) -> dict[str, int]:
    """Compute + upsert screener metrics for INTL companies.

    Returns row counts. Safe to run idempotently. When `limit` is passed the run
    is capped to the first N companies (by intl_company_id) — useful for smoke
    testing.
    """
    stats = {
        "companies": 0, "companies_written": 0, "metric_rows": 0, "market_cap_rows": 0,
        "ttm_rows": 0, "companies_with_ttm": 0,
    }
    with connect() as conn, conn.cursor() as cur:
        universe = _load_universe(cur, only_intl_company_ids)
        if limit:
            universe = universe[: int(limit)]
        stats["companies"] = len(universe)

        # Load FX rates once for all currencies present in the universe.
        fx_map = _load_fx_map(cur, [c.get("currency") for c in universe])

        all_metric_rows: list[tuple] = []
        all_mcap_rows: list[tuple[str, str, float, date | None]] = []
        for company in universe:
            statements, annual_ends = _load_statements(cur, company["intl_company_id"])
            quarters, quarter_ccys = _load_quarters(cur, company["intl_company_id"])
            profile = _load_profile(cur, company["intl_company_id"])
            rows, period_end, mcap_usd = _compute_one(
                company, statements, profile, fx_map=fx_map,
                annual_ends=annual_ends, quarters=quarters, quarter_currencies=quarter_ccys,
            )
            if rows:
                stats["companies_written"] += 1
                all_metric_rows.extend(rows)
                ttm_in_rows = sum(1 for r in rows if r[6] == "TTM")
                if ttm_in_rows:
                    stats["ttm_rows"] += ttm_in_rows
                    stats["companies_with_ttm"] += 1
                if mcap_usd is not None:
                    all_mcap_rows.append((company["ticker"], company["intl_company_id"], mcap_usd, period_end))
            if verbose and stats["companies_written"] % 100 == 0 and stats["companies_written"]:
                logger.info("compute_intl: %d/%d written", stats["companies_written"], stats["companies"])

        # Clear this run's stale TTM rows before re-inserting (see _delete_ttm).
        _delete_ttm(cur, [c["intl_company_id"] for c in universe])
        stats["metric_rows"] = _upsert_metrics(cur, all_metric_rows)
        stats["market_cap_rows"] = _upsert_market_cap(cur, all_mcap_rows)
    return stats


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Cap to first N companies (smoke test).")
    parser.add_argument("--ids", type=int, nargs="*", help="Restrict to specific intl_company_id values.")
    args = parser.parse_args()
    result = compute_intl_metrics(limit=args.limit, only_intl_company_ids=args.ids, verbose=True)
    print(f"stats: {result}")
