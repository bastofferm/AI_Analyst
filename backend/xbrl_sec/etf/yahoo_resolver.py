"""Resolve ETF ISINs to concrete Yahoo Finance quote symbols.

The resolver stages candidates first and promotes only high-confidence symbols
into the ETF profile/listing tables. Selenium is used for Yahoo search evidence;
yfinance is used only to validate that a quote symbol has usable price history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import re
import time
from typing import Any, Iterable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.sources.yahoo_identifier_enrichment import (
    DEFAULT_EDGE_BINARY_PATH,
    YahooQuoteCandidate,
    _search_yahoo_suggest,
    create_edge_driver,
    normalize_isin,
    search_yahoo_quotes,
)

from .prices import _configure_cache, _history_with_period_fallback


YAHOO_SUFFIX_TO_MIC: dict[str, str] = {
    ".DE": "XETR",
    ".F": "XFRA",
    ".SG": "XSTU",
    ".DU": "XDUS",
    ".MU": "XMUN",
    ".HM": "XHAM",
    ".VI": "XWBO",
}

YAHOO_REJECT_QUOTE_TYPES = {
    "CRYPTOCURRENCY",
    "EQUITY",
    "FUTURE",
    "INDEX",
    "OPTION",
}

YAHOO_ALLOWED_FUND_TYPES = {"ETF", "MUTUALFUND"}

TOKEN_STOPWORDS = {
    "ACC",
    "AG",
    "AND",
    "ANTEILE",
    "CLASS",
    "CORE",
    "DIST",
    "DR",
    "ETF",
    "ETFS",
    "EUR",
    "FUND",
    "FUNDS",
    "HEDGED",
    "INC",
    "INDEX",
    "INH",
    "ISH",
    "LTD",
    "NAMENS",
    "PLC",
    "REG",
    "SHARES",
    "THE",
    "UCITS",
    "UE",
    "USD",
}

PROFILE_INCOMPLETE_STATUSES = {None, "", "pending", "empty", "failed"}


@dataclass(frozen=True)
class EtfResolveTarget:
    isin: str
    full_name: str
    short_name: str | None
    issuer_name: str | None
    fund_family: str | None
    index_tracked: str | None
    asset_class: str | None
    fund_currency: str | None
    trading_currency: str | None
    primary_mic: str | None
    listing_mics: tuple[str, ...]
    current_yf_ticker: str | None
    profile_status: str | None
    aum_eur: float | None


@dataclass(frozen=True)
class PriceValidation:
    validated: bool
    price_rows: int = 0
    last_price_date: date | None = None
    currency: str | None = None
    error: str | None = None
    first_price_date: date | None = None


@dataclass(frozen=True)
class CandidateScore:
    score: float
    status: str
    status_reason: str
    components: dict[str, float]


@dataclass(frozen=True)
class ScoredYahooCandidate:
    target: EtfResolveTarget
    yahoo_candidate: YahooQuoteCandidate
    score: CandidateScore
    price_validation: PriceValidation | None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _dedupe_text(values: Iterable[tuple[str, str | None]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for strategy, value in values:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            continue
        key = re.sub(r"[^A-Z0-9]+", "", text.upper())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((strategy, text))
    return out


def normalize_fund_name(value: str | None) -> str:
    """Return a Yahoo-search-friendly fund name."""
    text = str(value or "")
    replacements = (
        (r"\bi\s*shs[ivx]*\b", "iShares"),
        (r"\bishs[ivx]*\b", "iShares"),
        (r"\bno\.?\s*am\.?\b", "North America"),
        (r"\bwld\b", "World"),
        (r"\bw\.?\s*sri\b", "World SRI"),
        (r"\bcl\.?\s*par\.?\s*alig\.?\b", "Climate Paris Aligned"),
        (r"\bpa\.?\s*al\.?\b", "Paris Aligned"),
        (r"\breg\.?\s*shs\b", "Shares"),
        (r"\bbear\.?\s*shs\b", "Shares"),
        (r"\bact\.?\s*port\.?\b", ""),
        (r"\beo\b", "Euro"),
        (r"\bcor\.?\s*bd\b", "Corporate Bond"),
        (r"\bcorp\b", "Corporate"),
        (r"\bfin\.?\s*u\.?\s*etf\b", "Financials UCITS ETF"),
        (r"\bfin\.?\s*ucits\b", "Financials UCITS"),
        (r"\bu\.?\s*etf\b", "UCITS ETF"),
        (r"\bfin\.?\b", "Financials"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"[\-_./]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9&+ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str | None) -> set[str]:
    text = normalize_fund_name(value).upper()
    raw = re.findall(r"[A-Z0-9]{3,}", text)
    return {token for token in raw if token not in TOKEN_STOPWORDS and not token.isdigit()}


def _token_similarity(left: str | None, right: str | None) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return (2.0 * overlap) / (len(left_tokens) + len(right_tokens))


def _contains_token(value: str | None, pattern: str) -> bool:
    return re.search(pattern, normalize_fund_name(value), flags=re.IGNORECASE) is not None


def _share_class(value: str | None) -> str | None:
    text = normalize_fund_name(value)
    if _contains_token(text, r"\b(acc|accumulation|accumulating)\b"):
        return "acc"
    if _contains_token(text, r"\b(dist|dis|income|distribution|distributing)\b"):
        return "dist"
    return None


def _share_class_label(value: str | None) -> str | None:
    share_class = _share_class(value)
    if share_class == "acc":
        return "Accumulation"
    if share_class == "dist":
        return "Income"
    return None


def _target_share_class(target: EtfResolveTarget) -> str | None:
    return _share_class(" ".join(part for part in (target.full_name, target.short_name) if part))


def _target_has_esg(target: EtfResolveTarget) -> bool:
    return _contains_token(" ".join(part for part in (target.full_name, target.short_name, target.index_tracked) if part), r"\b(esg|sri)\b")


def _index_aliases(index_name: str | None, *, include_esg: bool = False) -> list[str]:
    index = normalize_fund_name(index_name)
    if not index:
        return []
    base = re.sub(r"\b(Index|Benchmark)\b", "", index, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip()
    aliases: list[tuple[str, str | None]] = [("index", base)]

    stripped = base
    stripped = re.sub(r"\b(FTSE|MSCI|Bloomberg|ICE|Solactive|STOXX)\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\b(Choice|Select|Low Carbon)\b", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped:
        aliases.append(("index_stripped", stripped))
        if include_esg and "ESG" not in stripped.upper():
            aliases.append(("index_esg", f"ESG {stripped}"))

    climate = base
    climate = re.sub(r"\bChange\b", "", climate, flags=re.IGNORECASE)
    climate = re.sub(r"\bLow Carbon Select\b", "", climate, flags=re.IGNORECASE)
    climate = re.sub(r"\s+", " ", climate).strip()
    if climate:
        aliases.append(("index_climate", climate))
    return [text for _, text in _dedupe_text(aliases)]


def _target_name_aliases(target: EtfResolveTarget) -> list[str]:
    issuer = normalize_fund_name(target.issuer_name or target.fund_family)
    currency = str(target.fund_currency or target.trading_currency or "").strip().upper()
    share_label = _share_class_label(" ".join(part for part in (target.full_name, target.short_name) if part))
    aliases: list[tuple[str, str | None]] = [
        ("full_name", normalize_fund_name(target.full_name)),
        ("short_name", normalize_fund_name(target.short_name)),
    ]
    for index in _index_aliases(target.index_tracked, include_esg=_target_has_esg(target)):
        core = " ".join(part for part in (issuer, index, "UCITS ETF") if part)
        aliases.append(("natural_name", core))
        if currency:
            aliases.append(("natural_name_ccy", f"{core} {currency}"))
        if share_label:
            aliases.append(("natural_name_share", f"{core} {share_label}"))
            if currency:
                aliases.append(("natural_name_ccy_share", f"{core} {currency} {share_label}"))
        compact = re.sub(r"\b(Aligned|UCITS ETF)\b", "", core, flags=re.IGNORECASE)
        compact = re.sub(r"\s+", " ", compact).strip()
        if compact:
            aliases.append(("natural_name_compact", compact))
    return [text for _, text in _dedupe_text(aliases)]


def _is_isin_like(value: str | None) -> bool:
    text = str(value or "").strip().upper()
    return normalize_isin(text) is not None


def is_unresolved_yf_ticker(value: str | None, isin: str | None = None) -> bool:
    text = str(value or "").strip().upper()
    if not text:
        return True
    if isin and text == isin.upper():
        return True
    return _is_isin_like(text)


def yahoo_symbol_to_mic(symbol: str | None) -> str | None:
    text = str(symbol or "").strip().upper()
    for suffix, mic in YAHOO_SUFFIX_TO_MIC.items():
        if text.endswith(suffix):
            return mic
    return None


def yahoo_symbol_base(symbol: str | None) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    for suffix in YAHOO_SUFFIX_TO_MIC:
        if text.endswith(suffix):
            return text[: -len(suffix)] or None
    return text.split(".", 1)[0] or text


def _is_isin_yahoo_symbol(symbol: str | None) -> bool:
    base = yahoo_symbol_base(symbol)
    return normalize_isin(base) is not None


def build_etf_search_queries(target: EtfResolveTarget) -> list[tuple[str, str]]:
    clean_full = normalize_fund_name(target.full_name)
    clean_short = normalize_fund_name(target.short_name)
    issuer = normalize_fund_name(target.issuer_name or target.fund_family)
    index = normalize_fund_name(target.index_tracked)
    issuer_index = " ".join(part for part in (issuer, index, "ETF") if part)
    queries: list[tuple[str, str | None]] = [
            ("isin", target.isin),
            ("full_name", clean_full),
            ("issuer_index", issuer_index),
            ("short_name", clean_short),
    ]
    queries.extend(("natural_name", alias) for alias in _target_name_aliases(target))
    queries.append(("isin_etf", f"{target.isin} ETF"))
    return _dedupe_text(queries)


def _payload_row(candidate: YahooQuoteCandidate) -> dict[str, Any]:
    payload = candidate.raw_payload or {}
    row = payload.get("row")
    return row if isinstance(row, dict) else {}


def candidate_name(candidate: YahooQuoteCandidate) -> str | None:
    row = _payload_row(candidate)
    for key in ("longname", "longName", "shortname", "shortName", "name"):
        value = row.get(key)
        if value:
            return str(value)
    text = str((candidate.raw_payload or {}).get("text") or "").strip()
    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " ".join(lines[1:3]) if len(lines) > 1 else text
    return None


def candidate_exchange(candidate: YahooQuoteCandidate) -> str | None:
    row = _payload_row(candidate)
    return row.get("exchange") or row.get("exchDisp") or row.get("exchangeDisplay")


def candidate_currency(candidate: YahooQuoteCandidate) -> str | None:
    row = _payload_row(candidate)
    value = row.get("currency") or row.get("currencyCode")
    return str(value).upper() if value else None


def candidate_quote_type(candidate: YahooQuoteCandidate) -> str | None:
    row = _payload_row(candidate)
    value = row.get("quoteType") or row.get("typeDisp")
    return str(value).upper() if value else None


def _candidate_refinement_queries(candidate: YahooQuoteCandidate) -> list[tuple[str, str]]:
    """Use Yahoo's own display name to escape unpriced ISIN-style results.

    Yahoo search often returns an ISIN.SG fund stub first. The visible Yahoo UI
    then exposes the real exchange symbols when that stub's display name is
    searched again, e.g. FR0014015ZN2.SG -> MD4C.DE.
    """
    raw_name = re.sub(r"\s+", " ", str(candidate_name(candidate) or "").strip())
    name = normalize_fund_name(raw_name)
    if not raw_name or len(raw_name) < 8:
        return []
    quote_type = candidate_quote_type(candidate)
    should_refine = _is_isin_yahoo_symbol(candidate.symbol) or quote_type in YAHOO_ALLOWED_FUND_TYPES or quote_type in {"FUND"}
    if not should_refine:
        return []
    queries: list[tuple[str, str | None]] = [
        ("candidate_name_raw", raw_name),
        ("candidate_name", name),
    ]
    if "ETF" not in name.upper():
        queries.append(("candidate_name_etf", f"{name} ETF"))
    return _dedupe_text(queries)


def candidate_source_url(candidate: YahooQuoteCandidate) -> str:
    payload = candidate.raw_payload or {}
    href = payload.get("href")
    if href:
        return str(href)
    return f"https://finance.yahoo.com/quote/{candidate.symbol}"


def validate_yahoo_price(symbol: str, period: str = "1mo", min_rows: int = 2) -> PriceValidation:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("yfinance is not installed. Run project dependency setup first.") from exc
    _configure_cache(yf)
    ticker = yf.Ticker(symbol)
    df = _history_with_period_fallback(ticker, period)
    if df is None or df.empty or "Close" not in df.columns:
        return PriceValidation(False, 0, None, None, "no price data")
    frame = df.dropna(subset=["Close"])
    rows = int(len(frame))
    first_date = None
    last_date = None
    if rows:
        first_idx = frame.index[0]
        idx = frame.index[-1]
        first_date = first_idx.date() if hasattr(first_idx, "date") else first_idx
        last_date = idx.date() if hasattr(idx, "date") else idx
    currency = None
    try:
        fast_info = getattr(ticker, "fast_info", None)
        if fast_info is not None:
            currency = fast_info.get("currency") if hasattr(fast_info, "get") else getattr(fast_info, "currency", None)
    except Exception:
        currency = None
    return PriceValidation(rows >= min_rows, rows, last_date, str(currency).upper() if currency else None, None, first_date)


def score_yahoo_candidate(
    target: EtfResolveTarget,
    yahoo_candidate: YahooQuoteCandidate,
    *,
    price_validation: PriceValidation | None = None,
    min_score: float = 85.0,
) -> CandidateScore:
    components: dict[str, float] = {}
    quote_type = candidate_quote_type(yahoo_candidate)
    if quote_type in YAHOO_REJECT_QUOTE_TYPES:
        return CandidateScore(0.0, "rejected", f"Rejected Yahoo quote type: {quote_type}.", {"quote_type_reject": -100.0})

    payload_text = json.dumps(_json_safe(yahoo_candidate.raw_payload), sort_keys=True).upper()
    query_strategy = yahoo_candidate.query_strategy
    if target.isin.upper() in payload_text:
        components["isin_payload"] = 35.0
    elif query_strategy in {"isin", "isin_etf"}:
        components["isin_query"] = 24.0

    display_name = candidate_name(yahoo_candidate)
    target_aliases = _target_name_aliases(target)
    name_similarity = max((_token_similarity(alias, display_name) for alias in target_aliases), default=0.0)
    components["name_similarity"] = round(name_similarity * 30.0, 2)

    target_share = _target_share_class(target)
    candidate_share = _share_class(display_name)
    if target_share and candidate_share:
        if target_share == candidate_share:
            components["share_class_match"] = 12.0
        else:
            components["share_class_mismatch"] = -30.0

    issuer_text = " ".join(part for part in (target.issuer_name, target.fund_family) if part)
    issuer_overlap = _tokens(issuer_text) & _tokens(display_name)
    if issuer_overlap:
        components["issuer_match"] = 12.0

    if quote_type in YAHOO_ALLOWED_FUND_TYPES:
        components["fund_quote_type"] = 15.0
    elif quote_type is None:
        components["unknown_quote_type"] = 2.0

    mic = yahoo_symbol_to_mic(yahoo_candidate.symbol)
    if mic and mic in set(target.listing_mics):
        components["listing_suffix"] = 10.0
    elif mic:
        components["known_suffix"] = 6.0

    ccy = candidate_currency(yahoo_candidate)
    expected_ccys = {
        str(value).upper()
        for value in (target.trading_currency, target.fund_currency)
        if value
    }
    validation_ccy = price_validation.currency if price_validation and price_validation.currency else None
    if (ccy and ccy in expected_ccys) or (validation_ccy and validation_ccy in expected_ccys):
        components["currency_match"] = 8.0

    rank_bonus = max(0.0, 6.0 - float(yahoo_candidate.query_rank or 6))
    if rank_bonus:
        components["rank"] = min(rank_bonus, 5.0)

    if price_validation is not None and price_validation.validated:
        components["price_validated"] = 15.0

    score = min(100.0, round(sum(components.values()), 2))
    if price_validation is not None and not price_validation.validated:
        return CandidateScore(min(score, 64.0), "rejected", price_validation.error or "No validated Yahoo price history.", components)
    if price_validation is None:
        if score >= min_score:
            return CandidateScore(score, "review", "Price validation was not run.", components)
    elif score >= min_score:
        return CandidateScore(score, "accepted", "High-confidence Yahoo ETF symbol with validated prices.", components)
    if score >= 65.0:
        return CandidateScore(score, "review", "Candidate needs manual review.", components)
    return CandidateScore(score, "rejected", "Candidate score below review threshold.", components)


def select_resolution_targets(limit: int | None = None) -> list[EtfResolveTarget]:
    sql = """
        SELECT d.isin, d.full_name, d.short_name, d.issuer_name, p.fund_family,
               d.index_tracked, d.asset_class, d.fund_currency, listings.trading_currency,
               listings.primary_mic, COALESCE(listings.listing_mics, ARRAY[]::varchar[]),
               p.yf_ticker, p.profile_status,
               CASE WHEN d.aum_eur IS NULL THEN NULL ELSE d.aum_eur::double precision END AS aum_eur
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        LEFT JOIN LATERAL (
            SELECT
                (ARRAY_AGG(l.mic ORDER BY l.is_primary_listing DESC, (l.mic = 'XETR') DESC, l.mic))[1] AS primary_mic,
                (ARRAY_AGG(l.trading_currency ORDER BY l.is_primary_listing DESC, (l.mic = 'XETR') DESC, l.mic))[1] AS trading_currency,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT l.mic), NULL) AS listing_mics,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT NULLIF(BTRIM(l.exchange_ticker), '')), NULL) AS exchange_tickers
            FROM sec.dim_etf_listing l
            WHERE l.isin = d.isin
        ) listings ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS price_count
            FROM sec.fact_prices_etf fp
            WHERE fp.isin = d.isin
        ) prices ON TRUE
        WHERE COALESCE(d.is_active, TRUE)
          AND (
              COALESCE(prices.price_count, 0) = 0
              OR p.isin IS NULL
              OR p.yf_ticker IS NULL
              OR UPPER(p.yf_ticker) = UPPER(d.isin)
              OR COALESCE(p.profile_status, 'pending') IN ('pending', 'empty', 'failed')
              OR COALESCE(array_length(listings.exchange_tickers, 1), 0) = 0
          )
        ORDER BY
            (COALESCE(prices.price_count, 0) = 0) DESC,
            CASE
                WHEN lower(COALESCE(d.issuer_name, p.fund_family, d.full_name, '')) ~
                     '(xtrackers|amundi|ishares|invesco|ubs|global x|vanguard|franklin|wisdomtree|spdr|vaneck|hsbc)'
                THEN 0 ELSE 1
            END,
            d.aum_eur DESC NULLS LAST,
            d.isin
    """
    if limit is not None:
        sql += f"\n        LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return [
        EtfResolveTarget(
            isin=row[0],
            full_name=row[1],
            short_name=row[2],
            issuer_name=row[3],
            fund_family=row[4],
            index_tracked=row[5],
            asset_class=row[6],
            fund_currency=row[7],
            trading_currency=row[8],
            primary_mic=row[9],
            listing_mics=tuple(row[10] or ()),
            current_yf_ticker=row[11],
            profile_status=row[12],
            aum_eur=row[13],
        )
        for row in rows
    ]


def search_etf_quote_candidates(
    target: EtfResolveTarget,
    *,
    driver: Any | None = None,
    max_symbols_per_query: int = 8,
    wait_seconds: float = 6.0,
) -> list[YahooQuoteCandidate]:
    found: dict[str, YahooQuoteCandidate] = {}
    first_error: BaseException | None = None
    searched_keys: set[str] = set()

    def run_query(strategy: str, query: str) -> None:
        nonlocal first_error
        key = re.sub(r"[^A-Z0-9]+", "", f"{strategy}:{query}".upper())
        if not key or key in searched_keys:
            return
        searched_keys.add(key)
        try:
            if driver is not None:
                rows = search_yahoo_quotes(driver, query, strategy, max_symbols=max_symbols_per_query, wait_seconds=wait_seconds)
            else:
                rows = _search_yahoo_suggest(query, strategy, max_symbols=max_symbols_per_query)
        except Exception as exc:
            if first_error is None:
                first_error = exc
            return
        for candidate in rows:
            found.setdefault(candidate.symbol, candidate)

    for strategy, query in build_etf_search_queries(target):
        run_query(strategy, query)

    # Second pass: if the first pass found only Yahoo's ISIN/fund stubs, their
    # display names are usually the best query for the real .DE/.F/.SW symbols.
    for candidate in list(found.values()):
        for strategy, query in _candidate_refinement_queries(candidate):
            run_query(strategy, query)

    if not found and first_error is not None:
        raise first_error
    return list(found.values())


def score_candidates_for_target(
    target: EtfResolveTarget,
    yahoo_candidates: Iterable[YahooQuoteCandidate],
    *,
    validate_prices: bool = True,
    price_period: str = "1mo",
    min_score: float = 85.0,
) -> list[ScoredYahooCandidate]:
    scored: list[ScoredYahooCandidate] = []
    for candidate in yahoo_candidates:
        validation = None
        if validate_prices:
            try:
                validation = validate_yahoo_price(candidate.symbol, period=price_period)
            except Exception as exc:  # noqa: BLE001 - staged as failed evidence
                validation = PriceValidation(False, 0, None, None, f"{type(exc).__name__}: {exc}")
        score = score_yahoo_candidate(target, candidate, price_validation=validation, min_score=min_score)
        scored.append(ScoredYahooCandidate(target, candidate, score, validation))
    scored.sort(key=_candidate_promotion_sort_key, reverse=True)
    return scored


def _date_ordinal(value: date | None, fallback: int) -> int:
    return value.toordinal() if value is not None and hasattr(value, "toordinal") else fallback


def _candidate_promotion_sort_key(row: ScoredYahooCandidate) -> tuple[float, int, float, int, float, int, int]:
    """Rank candidates by usable history while preferring the ETF's own venues.

    A foreign Yahoo cross-listing can be a useful fallback when it has materially
    more history, but it should not beat a DE/AT listing by a handful of days.
    """
    validation = row.price_validation
    price_rows = validation.price_rows if validation and validation.validated else 0
    first_ord = _date_ordinal(validation.first_price_date if validation else None, date.max.toordinal())
    last_ord = _date_ordinal(validation.last_price_date if validation else None, date.min.toordinal())
    mic = yahoo_symbol_to_mic(row.yahoo_candidate.symbol)
    target = row.target
    exchange_rank = 0
    if mic and mic == target.primary_mic:
        exchange_rank = 2
    elif mic and mic in set(target.listing_mics):
        exchange_rank = 1
    effective_rows = float(price_rows if exchange_rank else price_rows * 0.97)
    # Higher is better. Negating first_ord means older first dates rank above
    # newer first dates when price row counts are equal.
    return (
        effective_rows,
        -first_ord,
        float(price_rows),
        last_ord,
        row.score.score,
        exchange_rank,
        -(row.yahoo_candidate.query_rank or 99),
    )


def select_best_candidate_for_promotion(
    rows: Iterable[ScoredYahooCandidate],
    *,
    min_score: float = 85.0,
    allow_review: bool = False,
) -> ScoredYahooCandidate | None:
    """Return the promotable candidate with the longest validated price history."""
    promotable: list[ScoredYahooCandidate] = []
    for row in rows:
        validation = row.price_validation
        if validation is None or not validation.validated:
            continue
        if row.score.score < min_score:
            continue
        if row.score.status == "accepted" or row.score.status == "promoted":
            promotable.append(row)
        elif allow_review and row.score.status == "review":
            promotable.append(row)
    if not promotable:
        return None
    return max(promotable, key=_candidate_promotion_sort_key)


def _candidate_evidence(row: ScoredYahooCandidate) -> dict[str, Any]:
    target = row.target
    candidate = row.yahoo_candidate
    validation = row.price_validation
    return {
        "target": {
            "isin": target.isin,
            "full_name": target.full_name,
            "short_name": target.short_name,
            "issuer_name": target.issuer_name,
            "fund_family": target.fund_family,
            "index_tracked": target.index_tracked,
            "asset_class": target.asset_class,
            "fund_currency": target.fund_currency,
            "trading_currency": target.trading_currency,
            "listing_mics": list(target.listing_mics),
            "current_yf_ticker": target.current_yf_ticker,
            "profile_status": target.profile_status,
            "aum_eur": target.aum_eur,
        },
        "query": {
            "strategy": candidate.query_strategy,
            "text": candidate.search_query,
            "rank": candidate.query_rank,
        },
        "candidate": {
            "symbol": candidate.symbol,
            "name": candidate_name(candidate),
            "exchange": candidate_exchange(candidate),
            "currency": candidate_currency(candidate),
            "quote_type": candidate_quote_type(candidate),
            "source_url": candidate_source_url(candidate),
            "raw_payload": candidate.raw_payload,
        },
        "score": {
            "value": row.score.score,
            "status": row.score.status,
            "status_reason": row.score.status_reason,
            "components": row.score.components,
        },
        "price_validation": None
        if validation is None
        else {
            "validated": validation.validated,
            "first_price_date": validation.first_price_date,
            "price_rows": validation.price_rows,
            "last_price_date": validation.last_price_date,
            "history_days": (
                (validation.last_price_date - validation.first_price_date).days
                if validation.first_price_date and validation.last_price_date
                else None
            ),
            "currency": validation.currency,
            "error": validation.error,
        },
    }


def upsert_staged_candidates(rows: Iterable[ScoredYahooCandidate]) -> int:
    payload = []
    for row in rows:
        candidate = row.yahoo_candidate
        validation = row.price_validation
        evidence = _candidate_evidence(row)
        payload.append(
            (
                row.target.isin,
                candidate.query_strategy,
                candidate.search_query,
                candidate.symbol,
                candidate_name(candidate),
                candidate_exchange(candidate),
                candidate_currency(candidate),
                candidate_quote_type(candidate),
                candidate_source_url(candidate),
                candidate.query_rank,
                row.score.score,
                row.score.status,
                row.score.status_reason,
                bool(validation.validated) if validation else False,
                int(validation.price_rows) if validation else 0,
                validation.last_price_date if validation else None,
                json.dumps(_json_safe(evidence), ensure_ascii=False, sort_keys=True),
                datetime.now(timezone.utc) if validation is not None else None,
            )
        )
    sql = """
        INSERT INTO sec.etf_yahoo_symbol_candidate
            (isin, query_strategy, query_text, candidate_symbol, candidate_name,
             candidate_exchange, candidate_currency, quote_type, source_url, rank,
             score, status, status_reason, price_validated, price_rows,
             last_price_date, evidence, validated_at)
        VALUES %s
        ON CONFLICT (isin, candidate_symbol, query_strategy, query_text) DO UPDATE SET
            candidate_name = EXCLUDED.candidate_name,
            candidate_exchange = EXCLUDED.candidate_exchange,
            candidate_currency = EXCLUDED.candidate_currency,
            quote_type = EXCLUDED.quote_type,
            source_url = EXCLUDED.source_url,
            rank = EXCLUDED.rank,
            score = EXCLUDED.score,
            status = CASE
                WHEN sec.etf_yahoo_symbol_candidate.status = 'promoted'
                THEN sec.etf_yahoo_symbol_candidate.status
                ELSE EXCLUDED.status
            END,
            status_reason = EXCLUDED.status_reason,
            price_validated = EXCLUDED.price_validated,
            price_rows = EXCLUDED.price_rows,
            last_price_date = EXCLUDED.last_price_date,
            evidence = EXCLUDED.evidence,
            searched_at = NOW(),
            validated_at = CASE WHEN EXCLUDED.validated_at IS NULL THEN NULL ELSE NOW() END,
            updated_at = NOW()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, payload, page_size=500)


def _mark_price_pending(cur: Any, isin: str) -> None:
    cur.execute(
        """
        INSERT INTO sec.pipeline_etf_state (isin, price_stage, last_run_at, error_message, retry_count)
        VALUES (%s, 'pending', NOW(), NULL, 0)
        ON CONFLICT (isin) DO UPDATE SET
            price_stage = 'pending',
            last_run_at = NOW(),
            error_message = NULL,
            retry_count = 0
        """,
        (isin,),
    )


def _load_staged_candidate(cur: Any, isin: str, symbol: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT isin, candidate_symbol, score::double precision, status, price_validated,
               candidate_name, evidence
        FROM sec.etf_yahoo_symbol_candidate
        WHERE isin = %s AND UPPER(candidate_symbol) = UPPER(%s)
        ORDER BY score DESC, updated_at DESC
        LIMIT 1
        """,
        (isin, symbol),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "isin": row[0],
        "symbol": row[1],
        "score": float(row[2] or 0),
        "status": row[3],
        "price_validated": bool(row[4]),
        "candidate_name": row[5],
        "evidence": row[6] or {},
    }


def promote_yahoo_candidate(
    isin: str,
    symbol: str,
    *,
    min_score: float = 85.0,
    allow_review: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, int | str]:
    isin = isin.strip().upper()
    symbol = symbol.strip().upper()
    with connect() as conn, conn.cursor() as cur:
        staged = _load_staged_candidate(cur, isin, symbol)
        if staged is None:
            return {"promoted": 0, "skipped": 1, "reason": "candidate_not_found"}
        status = str(staged["status"] or "")
        if not force:
            if float(staged["score"]) < min_score:
                return {"promoted": 0, "skipped": 1, "reason": "score_below_min"}
            if not bool(staged["price_validated"]):
                return {"promoted": 0, "skipped": 1, "reason": "price_not_validated"}
            if status not in {"accepted", "promoted"} and not allow_review:
                return {"promoted": 0, "skipped": 1, "reason": "status_not_promotable"}

        cur.execute(
            "SELECT yf_ticker, profile_status FROM sec.dim_etf_profile WHERE isin = %s",
            (isin,),
        )
        existing = cur.fetchone()
        current_ticker = existing[0] if existing else None
        current_status = existing[1] if existing else None
        if (
            existing
            and not force
            and str(current_status or "").lower() == "complete"
            and not is_unresolved_yf_ticker(current_ticker, isin)
            and str(current_ticker or "").upper() != symbol
        ):
            return {"promoted": 0, "skipped": 1, "reason": "complete_profile_has_symbol"}

        next_status = current_status
        if str(current_status or "").lower() in PROFILE_INCOMPLETE_STATUSES:
            next_status = "pending"
        if dry_run:
            return {"promoted": 0, "skipped": 0, "reason": "dry_run"}

        cur.execute(
            """
            INSERT INTO sec.dim_etf_profile (isin, clean_name, yf_ticker, profile_status, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (isin) DO UPDATE SET
                clean_name = COALESCE(sec.dim_etf_profile.clean_name, EXCLUDED.clean_name),
                yf_ticker = EXCLUDED.yf_ticker,
                profile_status = COALESCE(EXCLUDED.profile_status, sec.dim_etf_profile.profile_status),
                updated_at = NOW()
            """,
            (isin, staged["candidate_name"], symbol, next_status or "pending"),
        )

        mic = yahoo_symbol_to_mic(symbol)
        base = yahoo_symbol_base(symbol)
        if mic and base:
            cur.execute(
                """
                UPDATE sec.dim_etf_listing
                SET exchange_ticker = %s
                WHERE isin = %s
                  AND mic = %s
                  AND (exchange_ticker IS NULL OR BTRIM(exchange_ticker) = '')
                """,
                (base, isin, mic),
            )

        _mark_price_pending(cur, isin)
        cur.execute(
            """
            UPDATE sec.etf_yahoo_symbol_candidate
            SET status = CASE
                    WHEN price_validated AND score >= %s THEN 'accepted'
                    WHEN score >= 65 THEN 'review'
                    ELSE 'rejected'
                END,
                promoted_at = NULL,
                updated_at = NOW()
            WHERE isin = %s
              AND UPPER(candidate_symbol) <> UPPER(%s)
              AND status = 'promoted'
            """,
            (min_score, isin, symbol),
        )
        cur.execute(
            """
            UPDATE sec.etf_yahoo_symbol_candidate
            SET status = 'promoted', promoted_at = NOW(), updated_at = NOW()
            WHERE isin = %s AND UPPER(candidate_symbol) = UPPER(%s)
            """,
            (isin, symbol),
        )
    return {"promoted": 1, "skipped": 0, "reason": "promoted"}


def _load_staged_candidates_for_revalidation(
    *,
    limit: int | None = None,
) -> dict[str, list[YahooQuoteCandidate]]:
    scope_limit = f"LIMIT {int(limit)}" if limit is not None else ""
    sql = f"""
        WITH isin_scope AS (
            SELECT DISTINCT c.isin
            FROM sec.etf_yahoo_symbol_candidate c
            WHERE COALESCE(c.quote_type, '') NOT IN ('CRYPTOCURRENCY', 'EQUITY', 'FUTURE', 'INDEX', 'OPTION')
            ORDER BY c.isin
            {scope_limit}
        )
        SELECT c.isin, c.query_strategy, c.query_text, c.candidate_symbol, c.candidate_name,
               c.candidate_exchange, c.candidate_currency, c.quote_type, c.source_url,
               c.rank, c.evidence
        FROM sec.etf_yahoo_symbol_candidate c
        JOIN isin_scope s ON s.isin = c.isin
        WHERE COALESCE(c.quote_type, '') NOT IN ('CRYPTOCURRENCY', 'EQUITY', 'FUTURE', 'INDEX', 'OPTION')
        ORDER BY c.isin, c.score DESC, c.price_rows DESC, c.rank NULLS LAST
    """
    grouped: dict[str, list[YahooQuoteCandidate]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for (
            isin,
            query_strategy,
            query_text,
            symbol,
            name,
            exchange,
            currency,
            quote_type,
            source_url,
            rank,
            evidence,
        ) in cur.fetchall():
            key = (isin, str(symbol).upper(), str(query_strategy), str(query_text))
            if key in seen:
                continue
            seen.add(key)
            raw_payload = None
            if isinstance(evidence, dict):
                candidate_payload = evidence.get("candidate")
                if isinstance(candidate_payload, dict):
                    raw_payload = candidate_payload.get("raw_payload")
            if not isinstance(raw_payload, dict):
                raw_payload = {
                    "source": "staged_candidate",
                    "href": source_url,
                    "row": {
                        "symbol": symbol,
                        "shortname": name,
                        "exchange": exchange,
                        "currency": currency,
                        "quoteType": quote_type,
                    },
                }
            grouped.setdefault(isin, []).append(
                YahooQuoteCandidate(
                    symbol=symbol,
                    search_query=query_text,
                    query_strategy=query_strategy,
                    query_rank=rank,
                    raw_payload=raw_payload,
                )
            )
    return grouped


def _load_targets_by_isin(isins: Iterable[str]) -> dict[str, EtfResolveTarget]:
    isin_list = sorted({isin.strip().upper() for isin in isins if isin})
    if not isin_list:
        return {}
    sql = """
        SELECT d.isin, d.full_name, d.short_name, d.issuer_name, p.fund_family,
               d.index_tracked, d.asset_class, d.fund_currency, listings.trading_currency,
               listings.primary_mic, COALESCE(listings.listing_mics, ARRAY[]::varchar[]),
               p.yf_ticker, p.profile_status,
               CASE WHEN d.aum_eur IS NULL THEN NULL ELSE d.aum_eur::double precision END AS aum_eur
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        LEFT JOIN LATERAL (
            SELECT
                (ARRAY_AGG(l.mic ORDER BY l.is_primary_listing DESC, (l.mic = 'XETR') DESC, l.mic))[1] AS primary_mic,
                (ARRAY_AGG(l.trading_currency ORDER BY l.is_primary_listing DESC, (l.mic = 'XETR') DESC, l.mic))[1] AS trading_currency,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT l.mic), NULL) AS listing_mics
            FROM sec.dim_etf_listing l
            WHERE l.isin = d.isin
        ) listings ON TRUE
        WHERE d.isin = ANY(%s)
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (isin_list,))
        rows = cur.fetchall()
    return {
        row[0]: EtfResolveTarget(
            isin=row[0],
            full_name=row[1],
            short_name=row[2],
            issuer_name=row[3],
            fund_family=row[4],
            index_tracked=row[5],
            asset_class=row[6],
            fund_currency=row[7],
            trading_currency=row[8],
            primary_mic=row[9],
            listing_mics=tuple(row[10] or ()),
            current_yf_ticker=row[11],
            profile_status=row[12],
            aum_eur=row[13],
        )
        for row in rows
    }


def promote_best_yahoo_candidates(
    *,
    limit: int | None = None,
    min_score: float = 85.0,
    allow_review: bool = False,
    dry_run: bool = False,
    price_period: str = "max",
) -> dict[str, int]:
    grouped = _load_staged_candidates_for_revalidation(limit=limit)
    targets = _load_targets_by_isin(grouped)
    counts = {
        "targets": len(grouped),
        "revalidated": 0,
        "staged": 0,
        "promoted": 0,
        "skipped_promotions": 0,
        "no_promotable": 0,
        "errors": 0,
    }
    for isin, candidates in grouped.items():
        target = targets.get(isin)
        if target is None:
            counts["errors"] += 1
            continue
        try:
            scored = score_candidates_for_target(
                target,
                candidates,
                validate_prices=True,
                price_period=price_period,
                min_score=min_score,
            )
            counts["revalidated"] += len(scored)
            if scored and not dry_run:
                counts["staged"] += upsert_staged_candidates(scored)
            best = select_best_candidate_for_promotion(scored, min_score=min_score, allow_review=allow_review)
            if best is None:
                counts["no_promotable"] += 1
                continue
            if dry_run:
                counts["skipped_promotions"] += 1
                continue
            result = promote_yahoo_candidate(
                best.target.isin,
                best.yahoo_candidate.symbol,
                min_score=min_score,
                allow_review=allow_review,
            )
            counts["promoted"] += int(result.get("promoted", 0))
            counts["skipped_promotions"] += int(result.get("skipped", 0))
        except Exception:  # noqa: BLE001 - keep batch best-effort
            counts["errors"] += 1
    return counts


def run_yahoo_symbol_resolution(
    *,
    limit: int | None = None,
    apply: bool = False,
    min_score: float = 85.0,
    auto_promote: bool = True,
    use_selenium: bool = True,
    headless: bool = True,
    edge_binary_path: str = DEFAULT_EDGE_BINARY_PATH,
    driver_path: str | None = None,
    max_symbols_per_query: int = 8,
    wait_seconds: float = 6.0,
    sleep_seconds: float = 0.5,
    validate_prices: bool = True,
    price_period: str = "max",
) -> dict[str, int]:
    targets = select_resolution_targets(limit=limit)
    counts = {
        "targets": len(targets),
        "searched": 0,
        "candidates": 0,
        "staged": 0,
        "accepted": 0,
        "review": 0,
        "rejected": 0,
        "promoted": 0,
        "skipped_promotions": 0,
        "errors": 0,
    }
    if not targets:
        return counts

    driver = None
    if use_selenium:
        driver = create_edge_driver(headless=headless, edge_binary_path=edge_binary_path, driver_path=driver_path)
    try:
        for idx, target in enumerate(targets, start=1):
            try:
                yahoo_candidates = search_etf_quote_candidates(
                    target,
                    driver=driver,
                    max_symbols_per_query=max_symbols_per_query,
                    wait_seconds=wait_seconds,
                )
                counts["searched"] += 1
                scored = score_candidates_for_target(
                    target,
                    yahoo_candidates,
                    validate_prices=validate_prices,
                    price_period=price_period,
                    min_score=min_score,
                )
                counts["candidates"] += len(scored)
                for row in scored:
                    counts[row.score.status] = counts.get(row.score.status, 0) + 1
                if apply and scored:
                    counts["staged"] += upsert_staged_candidates(scored)
                    if auto_promote:
                        best = select_best_candidate_for_promotion(scored, min_score=min_score)
                        if best is not None:
                            result = promote_yahoo_candidate(
                                best.target.isin,
                                best.yahoo_candidate.symbol,
                                min_score=min_score,
                            )
                            counts["promoted"] += int(result.get("promoted", 0))
                            counts["skipped_promotions"] += int(result.get("skipped", 0))
            except Exception:  # noqa: BLE001 - keep batch best-effort
                counts["errors"] += 1
            if sleep_seconds > 0 and idx < len(targets):
                time.sleep(sleep_seconds)
    finally:
        if driver is not None:
            driver.quit()
    return counts
