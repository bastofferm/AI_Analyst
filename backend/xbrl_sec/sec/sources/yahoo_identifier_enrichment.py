"""Yahoo Finance identifier enrichment for 13F CUSIP securities."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()

DEFAULT_EDGE_BINARY_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
SOURCE_NAME = "yahoo_finance.selenium_yfinance"
PRICE_SOURCE_NAME = "yahoo_finance.price_coverage"
_BI_SUGGEST_URL = "https://markets.businessinsider.com/ajax/SearchController_Suggest?max_results=25&query={q}"
_YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount={count}&newsCount=0&quotesQueryId=tss_match_phrase_query"
_YAHOO_CHART_RANGE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d&events=history&includeAdjustedClose=true"
_YAHOO_CHART_PERIOD_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
OPTION_TITLES = ("CALL", "PUT", "WRNT", "WTS", "OPTION")
MATCH_STATUSES = ("accepted", "ticker_only", "conflict", "not_found", "error")
QUOTE_SYMBOL_RE = re.compile(r"/quote/([^/?#]+)")
BI_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{10}")
_EVIDENCE_TABLE_READY = False
_PRICE_TABLE_READY = False

ISSUER_TOKEN_STOPWORDS = {
    "A", "AG", "AND", "CL", "CLASS", "CO", "COM", "COMMON", "COMPANY", "CORP", "CORPORATION",
    "ETF", "ETFS", "FD", "FDS", "FUND", "FUNDS", "GROUP", "HLDG", "HLDGS", "HOLDING", "HOLDINGS",
    "INC", "INCORPORATED", "LTD", "NEW", "NV", "PLC", "PORTFOLIO", "REG", "REGISTERED", "SA",
    "SEC", "SECTOR", "SER", "SERIES", "SH", "SHARE", "SHARES", "SHS", "THE", "TR", "TRUST",
    "TRUSTS", "USD",
}
GENERIC_SINGLE_MATCH_TOKENS = {
    "AMERICAN", "CAPITAL", "ENERGY", "FINANCIAL", "FIRST", "GLOBAL", "GROWTH", "HIGH", "INCOME",
    "INDEX", "INTL", "INTERNATIONAL", "MARKET", "MUNICIPAL", "NATIONAL", "REALTY", "SELECT", "SMALL",
    "STATE", "UNITED", "VALUE",
}
ISSUER_ALIAS_TOKENS = {"FACEBOOK": ("META", "PLATFORMS"), "GOOGLE": ("ALPHABET",)}
AUTOCOMPLETE_TEXT_EXCLUDE = {
    "CRYPTO", "ETF", "EQUITY", "FUTURE", "FUTURES", "INDEX", "LOOKUP", "MUTUAL", "NMS", "NYQ", "PCX",
    "QUOTE", "SCREENER", "SYMBOL", "SYMBOLS",
}
YAHOO_ALLOWED_QUOTE_TYPES = {"EQUITY", "ETF", "MUTUALFUND"}
HTTP_RETRY_ATTEMPTS = int(os.environ.get("YAHOO_IDENTIFIER_HTTP_RETRIES", "4"))
HTTP_RETRY_BACKOFF_SECONDS = float(os.environ.get("YAHOO_IDENTIFIER_HTTP_BACKOFF_SECONDS", "0.75"))


def _read_url_with_retries(req: Request, timeout: float = 10.0) -> bytes:
    attempts = max(1, HTTP_RETRY_ATTEMPTS)
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(HTTP_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


@dataclass(frozen=True)
class SecurityCandidate:
    cusip: str
    issuer_name: str | None
    security_title: str | None
    primary_ticker: str | None
    asset_bucket: str | None
    resolution_status: str | None
    row_count: int
    value_observed: float | None


@dataclass(frozen=True)
class YahooQuoteCandidate:
    symbol: str
    search_query: str
    query_strategy: str
    query_rank: int
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class MatchDecision:
    status: str
    status_reason: str
    confidence_score: float


@dataclass(frozen=True)
class EnrichmentEvidence:
    cusip: str
    yahoo_symbol: str
    search_query: str | None
    query_strategy: str | None
    query_rank: int | None
    issuer_name: str | None
    security_title: str | None
    asset_bucket: str | None
    discovered_ticker: str | None
    discovered_isin: str | None
    yahoo_short_name: str | None
    yahoo_long_name: str | None
    yahoo_exchange: str | None
    yahoo_quote_type: str | None
    sector: str | None
    industry_group: str | None
    confidence_score: float
    status: str
    status_reason: str
    error_type: str | None
    error_message: str | None


def candidate_where_sql(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    excluded = ", ".join(f"'{title}'" for title in OPTION_TITLES)
    return f"{prefix}isin IS NULL\n  AND {prefix}security_title NOT IN ({excluded})\n  AND {prefix}asset_bucket <> 'fixed_income'"


def normalize_cusip(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return text if re.fullmatch(r"[A-Z0-9]{9}", text) else None


def normalize_isin(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return text if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", text) else None


def isin_checksum_valid(value: Any) -> bool:
    isin = normalize_isin(value)
    if isin is None:
        return False
    expanded = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in isin)
    total = 0
    double = False
    for ch in reversed(expanded):
        digit = int(ch)
        if double:
            digit *= 2
        total += digit // 10 + digit % 10
        double = not double
    return total % 10 == 0


def isin_matches_cusip(isin: Any, cusip: Any) -> bool:
    norm_isin = normalize_isin(isin)
    norm_cusip = normalize_cusip(cusip)
    return bool(norm_isin and norm_cusip and isin_checksum_valid(norm_isin) and norm_isin[2:11] == norm_cusip)


def classify_match(cusip: str, yahoo_symbol: str | None, discovered_isin: str | None) -> MatchDecision:
    symbol = (yahoo_symbol or "").strip()
    isin = normalize_isin(discovered_isin)
    if not symbol:
        return MatchDecision("not_found", "No Yahoo quote symbol found.", 0.0)
    if discovered_isin in (None, "", "-") or str(discovered_isin).strip() == "-":
        return MatchDecision("ticker_only", "Yahoo symbol found, but yfinance did not return an ISIN.", 45.0)
    if isin is None or not isin_checksum_valid(isin):
        return MatchDecision("conflict", "Identifier source returned an invalid ISIN.", 25.0)
    if not isin_matches_cusip(isin, cusip):
        return MatchDecision("conflict", "Discovered ISIN does not match the CUSIP.", 15.0)
    return MatchDecision("accepted", "Validated ISIN body matches CUSIP.", 100.0)


def build_search_queries(candidate: SecurityCandidate) -> list[tuple[str, str]]:
    raw = [
        ("cusip", candidate.cusip),
        ("primary_ticker", candidate.primary_ticker),
        ("issuer_title", " ".join(v for v in (candidate.issuer_name, candidate.security_title) if v)),
        ("issuer_name", candidate.issuer_name),
    ]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for strategy, query in raw:
        query_text = (query or "").strip()
        query_key = re.sub(r"\s+", " ", query_text).upper()
        if query_key and query_key not in seen:
            seen.add(query_key)
            out.append((strategy, query_text))
    return out


def _text_tokens(value: Any) -> list[str]:
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "").upper())
    return [token for token in text.split() if len(token) >= 3 and not token.isdigit() and token not in ISSUER_TOKEN_STOPWORDS]


def _expand_issuer_tokens(tokens: list[str]) -> list[str]:
    expanded = list(tokens)
    seen = set(expanded)
    for token in tokens:
        for alias in ISSUER_ALIAS_TOKENS.get(token, ()):
            if alias not in seen:
                expanded.append(alias)
                seen.add(alias)
    return expanded


def _symbol_key(symbol: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())


def _clean_symbol(symbol: str) -> str | None:
    text = unquote(str(symbol or "")).strip().upper()
    if not text or text in {"_", "SEARCH", "LOOKUP"}:
        return None
    if text.startswith("^") or "=" in text:
        return None
    if re.search(r"[^A-Z0-9.\-=\^]", text):
        return None
    return text


def metadata_relevant_to_candidate(candidate: SecurityCandidate, symbol: str, yfinance_metadata: dict[str, Any]) -> bool:
    if isin_matches_cusip(yfinance_metadata.get("isin"), candidate.cusip):
        return True
    clean_symbol = _clean_symbol(symbol) or symbol.upper()
    if candidate.primary_ticker and _symbol_key(clean_symbol) == _symbol_key(candidate.primary_ticker):
        return True
    issuer_tokens = _expand_issuer_tokens(_text_tokens(candidate.issuer_name))
    if not issuer_tokens:
        return False
    meta_text = " ".join(str(yfinance_metadata.get(key) or "") for key in ("symbol", "shortName", "longName")).upper()
    meta_tokens = set(_text_tokens(meta_text))
    if clean_symbol in issuer_tokens:
        return True
    overlap = {token for token in issuer_tokens if token in meta_tokens}
    if len(overlap) >= 2:
        return True
    if len(overlap) == 1:
        token = next(iter(overlap))
        return len(token) >= 5 and token not in GENERIC_SINGLE_MATCH_TOKENS
    return False


def _symbol_from_url(url: str) -> str | None:
    match = QUOTE_SYMBOL_RE.search(url or "")
    return _clean_symbol(match.group(1)) if match else None


def _symbol_from_autocomplete_text(text: str) -> str | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in lines[:4]:
        symbol = _clean_symbol(line.split()[0].strip())
        if symbol and symbol not in AUTOCOMPLETE_TEXT_EXCLUDE and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
            return symbol
    return None


def _iter_bi_isin_segments(data: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for match in re.finditer(r'"([^"]*[A-Z]{2}[A-Z0-9]{10}[^"]*)"', data or ""):
        segment = match.group(1)
        for isin_match in BI_ISIN_RE.finditer(segment):
            isin = normalize_isin(isin_match.group(0))
            if isin and isin_checksum_valid(isin):
                segments.append((segment, isin))
    return segments


def _bi_segment_matches_ticker(segment: str, ticker: str) -> bool:
    ticker_key = _symbol_key(ticker)
    return bool(ticker_key and any(_symbol_key(part) == ticker_key for part in segment.split("|")))


def _parse_bi_isin_response(ticker: str, data: str, target_cusip: str | None = None) -> str | None:
    ticker_text = (ticker or "").strip().upper()
    if not ticker_text:
        return None
    segments = _iter_bi_isin_segments(data)
    norm_target = normalize_cusip(target_cusip)
    if norm_target:
        target_matches = [isin for _, isin in segments if isin_matches_cusip(isin, norm_target)]
        ticker_segments = [(segment, isin) for segment, isin in segments if _bi_segment_matches_ticker(segment, ticker_text)]
        for _, isin in ticker_segments:
            if isin_matches_cusip(isin, norm_target):
                return isin
        if ticker_segments:
            return None
        return target_matches[0] if len(set(target_matches)) == 1 else None
    for segment, isin in segments:
        if _bi_segment_matches_ticker(segment, ticker_text):
            return isin
    return None


def _bi_symbol_from_segment_part(part: str) -> str | None:
    text = re.sub(r"<[^>]+>", "", str(part or "")).strip().strip("'\"").upper().replace(" ", "")
    if not text or BI_ISIN_RE.fullmatch(text):
        return None
    symbol = _clean_symbol(text)
    if not symbol or symbol in AUTOCOMPLETE_TEXT_EXCLUDE:
        return None
    digit_count = sum(ch.isdigit() for ch in symbol)
    if "." not in symbol and "-" not in symbol and digit_count > 3:
        return None
    if normalize_cusip(symbol) and digit_count > 3:
        return None
    if re.fullmatch(r"[A-Z]{1,5}\.[A-Z]", symbol):
        return symbol.replace(".", "-")
    return symbol


def _bi_quote_candidates_from_response(data: str, search_query: str, query_strategy: str, max_symbols: int, target_cusip: str | None = None) -> list[YahooQuoteCandidate]:
    norm_target = normalize_cusip(target_cusip)
    candidates: list[YahooQuoteCandidate] = []
    seen_keys: set[str] = set()
    for segment, isin in _iter_bi_isin_segments(data):
        if norm_target and not isin_matches_cusip(isin, norm_target):
            continue
        for part in segment.split("|"):
            symbol = _bi_symbol_from_segment_part(part)
            symbol_key = _symbol_key(symbol)
            if not symbol or not symbol_key or symbol_key in seen_keys:
                continue
            seen_keys.add(symbol_key)
            candidates.append(YahooQuoteCandidate(symbol, search_query, query_strategy, len(candidates) + 1, {"source": "businessinsider_suggest", "segment": segment, "isin": isin}))
            if len(candidates) >= max_symbols:
                return candidates
    return candidates


def _search_business_insider_suggest(search_query: str, query_strategy: str, max_symbols: int, target_cusip: str | None = None) -> list[YahooQuoteCandidate]:
    req = Request(_BI_SUGGEST_URL.format(q=quote(search_query)), headers={"User-Agent": "Mozilla/5.0"})
    data = _read_url_with_retries(req, timeout=10).decode("utf-8", errors="ignore")
    return _bi_quote_candidates_from_response(data, search_query, query_strategy, max_symbols, target_cusip=target_cusip)


def _bi_fetch_isin(ticker: str, *queries: str | None, target_cusip: str | None = None) -> str | None:
    seen: set[str] = set()
    for query in (*queries, ticker):
        query_text = str(query or "").strip()
        query_key = query_text.upper()
        if not query_text or query_key in seen:
            continue
        seen.add(query_key)
        req = Request(_BI_SUGGEST_URL.format(q=quote(query_text)), headers={"User-Agent": "Mozilla/5.0"})
        data = _read_url_with_retries(req, timeout=10).decode("utf-8", errors="ignore")
        isin = _parse_bi_isin_response(ticker, data, target_cusip=target_cusip)
        if isin:
            return isin
    return None


def _search_yahoo_suggest(search_query: str, query_strategy: str, max_symbols: int) -> list[YahooQuoteCandidate]:
    req = Request(_YAHOO_SEARCH_URL.format(q=quote(search_query), count=max(max_symbols, 5)), headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(_read_url_with_retries(req, timeout=10).decode("utf-8", errors="ignore"))
    query_cusip = normalize_cusip(search_query)
    candidates: list[YahooQuoteCandidate] = []
    seen: set[str] = set()
    for row in data.get("quotes", []):
        quote_type = str(row.get("quoteType") or "").upper()
        if quote_type and quote_type not in YAHOO_ALLOWED_QUOTE_TYPES:
            continue
        symbol = _clean_symbol(str(row.get("symbol") or ""))
        if symbol is None or symbol == query_cusip or symbol in seen:
            continue
        seen.add(symbol)
        candidates.append(YahooQuoteCandidate(symbol, search_query, query_strategy, len(candidates) + 1, {"source": "yahoo_search_api", "row": row}))
        if len(candidates) >= max_symbols:
            break
    return candidates


def _known_isin_from_quote_candidate(yahoo_candidate: YahooQuoteCandidate) -> tuple[str | None, str | None]:
    raw_payload = yahoo_candidate.raw_payload or {}
    isin = normalize_isin(raw_payload.get("isin"))
    if not isin:
        return None, None
    source = str(raw_payload.get("source") or "external")
    return isin, "businessinsider" if source == "businessinsider_suggest" else source


def _metadata_from_quote_candidate_payload(yahoo_candidate: YahooQuoteCandidate) -> dict[str, Any]:
    raw_payload = yahoo_candidate.raw_payload or {}
    row = raw_payload.get("row") if isinstance(raw_payload.get("row"), dict) else {}
    meta = {
        "symbol": yahoo_candidate.symbol,
        "shortName": row.get("shortname") or row.get("shortName") or row.get("name"),
        "longName": row.get("longname") or row.get("longName"),
        "exchange": row.get("exchange") or row.get("exchDisp"),
        "quoteType": row.get("quoteType"),
        "sector": None,
        "industry": None,
    }
    known_isin, known_isin_source = _known_isin_from_quote_candidate(yahoo_candidate)
    if known_isin:
        meta["isin"] = known_isin
        meta["isinSource"] = known_isin_source
    return meta


def quote_candidate_relevant_to_candidate(candidate: SecurityCandidate, yahoo_candidate: YahooQuoteCandidate) -> bool:
    known_isin, _ = _known_isin_from_quote_candidate(yahoo_candidate)
    if known_isin and isin_matches_cusip(known_isin, candidate.cusip):
        return True
    if candidate.primary_ticker and _symbol_key(yahoo_candidate.symbol) == _symbol_key(candidate.primary_ticker):
        return True
    raw_payload = yahoo_candidate.raw_payload or {}
    if raw_payload.get("source") == "businessinsider_suggest":
        return False
    meta = _metadata_from_quote_candidate_payload(yahoo_candidate)
    if any(meta.get(key) for key in ("shortName", "longName")):
        return metadata_relevant_to_candidate(candidate, yahoo_candidate.symbol, meta)
    return True


def fetch_yfinance_metadata(symbol: str, target_cusip: str | None = None, known_isin: str | None = None, known_isin_source: str | None = None) -> dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Run project dependency setup first.") from exc

    ticker = yf.Ticker(symbol)
    info: dict[str, Any] = {}
    try:
        info = ticker.info or {}
    except Exception as exc:
        info = {"_info_error": f"{type(exc).__name__}: {exc}"}
    try:
        isin = ticker.isin
    except Exception as exc:
        isin = None
        info["_isin_error"] = f"{type(exc).__name__}: {exc}"

    isin_source = "yfinance" if normalize_isin(isin) else None
    if target_cusip is not None and isin_source and not isin_matches_cusip(isin, target_cusip):
        isin_source = None

    normalized_known_isin = normalize_isin(known_isin)
    if normalized_known_isin and (target_cusip is None or isin_matches_cusip(normalized_known_isin, target_cusip)) and not isin_source:
        isin = normalized_known_isin
        isin_source = known_isin_source or "external"

    if isin_source is None:
        info["_bi_isin_attempted"] = True
        try:
            via_bi = _bi_fetch_isin(symbol, info.get("shortName"), info.get("longName"), target_cusip=target_cusip)
        except Exception as exc:
            via_bi = None
            info["_bi_isin_error"] = f"{type(exc).__name__}: {exc}"
        if via_bi:
            isin = via_bi
            isin_source = "businessinsider"
        else:
            try:
                via_bi = _bi_fetch_isin(symbol, info.get("shortName"), info.get("longName"), target_cusip=None)
            except Exception as exc:
                via_bi = None
                info["_bi_isin_error"] = f"{type(exc).__name__}: {exc}"
            if via_bi:
                isin = via_bi
                isin_source = "businessinsider"
                info["_bi_isin_result"] = "found_nonmatching"
            else:
                info["_bi_isin_result"] = "not_found"

    if isin_source:
        info["_isin_source"] = isin_source
    return {
        "symbol": info.get("symbol") or symbol,
        "isin": isin,
        "isinSource": isin_source,
        "shortName": info.get("shortName"),
        "longName": info.get("longName"),
        "exchange": info.get("exchange"),
        "quoteType": info.get("quoteType"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "biIsinAttempted": bool(info.get("_bi_isin_attempted")),
        "biIsinError": info.get("_bi_isin_error"),
    }


def build_evidence(candidate: SecurityCandidate, yahoo_candidate: YahooQuoteCandidate | None, yfinance_metadata: dict[str, Any] | None, error: BaseException | None = None) -> EnrichmentEvidence:
    if error is not None:
        return EnrichmentEvidence(
            candidate.cusip,
            yahoo_candidate.symbol if yahoo_candidate else "",
            yahoo_candidate.search_query if yahoo_candidate else None,
            yahoo_candidate.query_strategy if yahoo_candidate else None,
            yahoo_candidate.query_rank if yahoo_candidate else None,
            candidate.issuer_name,
            candidate.security_title,
            candidate.asset_bucket,
            yahoo_candidate.symbol if yahoo_candidate else None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0.0,
            "error",
            "Exception while enriching candidate.",
            type(error).__name__,
            str(error),
        )
    if yahoo_candidate is None:
        decision = classify_match(candidate.cusip, None, None)
        return EnrichmentEvidence(candidate.cusip, "", None, None, None, candidate.issuer_name, candidate.security_title, candidate.asset_bucket, None, None, None, None, None, None, None, None, decision.confidence_score, decision.status, decision.status_reason, None, None)

    meta = yfinance_metadata or {}
    discovered_ticker = str(meta.get("symbol") or yahoo_candidate.symbol)
    discovered_isin = normalize_isin(meta.get("isin")) or (str(meta.get("isin")) if meta.get("isin") else None)
    decision = classify_match(candidate.cusip, discovered_ticker, discovered_isin)
    status_reason = decision.status_reason
    if decision.status == "ticker_only":
        if meta.get("biIsinError"):
            status_reason = "Yahoo symbol found, but yfinance did not return an ISIN and Business Insider fallback errored."
        elif meta.get("biIsinAttempted"):
            status_reason = "Yahoo symbol found, but neither yfinance nor Business Insider returned a CUSIP-matching ISIN."
    return EnrichmentEvidence(
        candidate.cusip,
        yahoo_candidate.symbol,
        yahoo_candidate.search_query,
        yahoo_candidate.query_strategy,
        yahoo_candidate.query_rank,
        candidate.issuer_name,
        candidate.security_title,
        candidate.asset_bucket,
        discovered_ticker,
        normalize_isin(discovered_isin),
        meta.get("shortName"),
        meta.get("longName"),
        meta.get("exchange"),
        meta.get("quoteType"),
        meta.get("sector"),
        meta.get("industry"),
        decision.confidence_score,
        decision.status,
        status_reason,
        None,
        None,
    )


def ensure_evidence_table() -> None:
    from xbrl_sec.sec.db.connection import connect

    global _EVIDENCE_TABLE_READY
    if _EVIDENCE_TABLE_READY:
        return
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "065_13f_yahoo_identifier_enrichment.sql"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql_path.read_text(encoding="utf-8-sig"))
    _EVIDENCE_TABLE_READY = True


def ensure_13f_price_table() -> None:
    from xbrl_sec.sec.db.connection import connect

    global _PRICE_TABLE_READY
    if _PRICE_TABLE_READY:
        return
    sql_path = Path(__file__).resolve().parents[1] / "sql" / "066_13f_yahoo_prices.sql"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql_path.read_text(encoding="utf-8-sig"))
    _PRICE_TABLE_READY = True


def select_candidates(limit: int | None = None, resume: bool = False, apply: bool = False) -> list[SecurityCandidate]:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    resume_sql = ""
    if resume:
        accepted_clause = "e.applied" if apply else "TRUE"
        resume_sql = f"""
          AND NOT EXISTS (
              SELECT 1
              FROM fact_13f_yahoo_identifier_enrichment e
              WHERE e.cusip = d.cusip
                AND (
                    e.status IN ('ticker_only', 'conflict', 'not_found')
                    OR (e.status = 'accepted' AND {accepted_clause})
                )
          )
        """
    limit_sql = "LIMIT %s" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.cusip, d.issuer_name, d.security_title, d.primary_ticker,
                   d.asset_bucket, d.resolution_status, d.row_count, d.value_observed
            FROM dim_13f_security_us d
            WHERE {candidate_where_sql("d")}
            {resume_sql}
            ORDER BY COALESCE(d.value_observed, 0) DESC, d.row_count DESC, d.cusip
            {limit_sql}
            """,
            params,
        )
        rows = cur.fetchall()
    return [SecurityCandidate(row[0], row[1], row[2], row[3], row[4], row[5], int(row[6] or 0), float(row[7]) if row[7] is not None else None) for row in rows]


_UPSERT_EVIDENCE_SQL = """
INSERT INTO fact_13f_yahoo_identifier_enrichment
    (cusip, yahoo_symbol, search_query, query_strategy, query_rank, issuer_name,
     security_title, asset_bucket, discovered_ticker, discovered_isin,
     yahoo_short_name, yahoo_long_name, yahoo_exchange, yahoo_quote_type,
     sector, industry_group, confidence_score, status, status_reason,
     error_type, error_message)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (cusip, yahoo_symbol) DO UPDATE SET
    search_query = EXCLUDED.search_query,
    query_strategy = EXCLUDED.query_strategy,
    query_rank = EXCLUDED.query_rank,
    issuer_name = EXCLUDED.issuer_name,
    security_title = EXCLUDED.security_title,
    asset_bucket = EXCLUDED.asset_bucket,
    discovered_ticker = EXCLUDED.discovered_ticker,
    discovered_isin = EXCLUDED.discovered_isin,
    yahoo_short_name = EXCLUDED.yahoo_short_name,
    yahoo_long_name = EXCLUDED.yahoo_long_name,
    yahoo_exchange = EXCLUDED.yahoo_exchange,
    yahoo_quote_type = EXCLUDED.yahoo_quote_type,
    sector = EXCLUDED.sector,
    industry_group = EXCLUDED.industry_group,
    confidence_score = EXCLUDED.confidence_score,
    status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason,
    error_type = EXCLUDED.error_type,
    error_message = EXCLUDED.error_message,
    updated_at = now()
"""


def _evidence_params(evidence: EnrichmentEvidence) -> tuple[Any, ...]:
    return (
        evidence.cusip,
        evidence.yahoo_symbol or "",
        evidence.search_query,
        evidence.query_strategy,
        evidence.query_rank,
        evidence.issuer_name,
        evidence.security_title,
        evidence.asset_bucket,
        evidence.discovered_ticker,
        evidence.discovered_isin,
        evidence.yahoo_short_name,
        evidence.yahoo_long_name,
        evidence.yahoo_exchange,
        evidence.yahoo_quote_type,
        evidence.sector,
        evidence.industry_group,
        evidence.confidence_score,
        evidence.status,
        evidence.status_reason,
        evidence.error_type,
        evidence.error_message,
    )


def upsert_evidence(evidence: EnrichmentEvidence, apply: bool = False) -> int:
    from psycopg2.extras import Json
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    applied = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_UPSERT_EVIDENCE_SQL, _evidence_params(evidence))
        if apply and evidence.status == "accepted" and evidence.discovered_isin and evidence.discovered_ticker:
            payload = {
                "source_name": SOURCE_NAME,
                "yahoo_symbol": evidence.yahoo_symbol,
                "discovered_ticker": evidence.discovered_ticker,
                "discovered_isin": evidence.discovered_isin,
                "search_query": evidence.search_query,
                "query_strategy": evidence.query_strategy,
                "confidence_score": evidence.confidence_score,
                "status_reason": evidence.status_reason,
                "yahoo_short_name": evidence.yahoo_short_name,
                "yahoo_long_name": evidence.yahoo_long_name,
                "yahoo_exchange": evidence.yahoo_exchange,
                "yahoo_quote_type": evidence.yahoo_quote_type,
            }
            cur.execute(
                """
                UPDATE dim_13f_security_us
                SET isin = %s,
                    primary_ticker = %s,
                    resolution_status = 'resolved',
                    source_name = %s,
                    confidence_score = %s,
                    sector = COALESCE(%s, sector),
                    industry_group = COALESCE(%s, industry_group),
                    evidence_payload = COALESCE(evidence_payload, '{}'::jsonb)
                        || jsonb_build_object('yahoo_finance', %s::jsonb),
                    updated_at = now()
                WHERE cusip = %s
                  AND isin IS NULL
                """,
                (evidence.discovered_isin, evidence.discovered_ticker, SOURCE_NAME, evidence.confidence_score, evidence.sector, evidence.industry_group, Json(payload), evidence.cusip),
            )
            applied = cur.rowcount
            if applied:
                cur.execute(
                    """
                    UPDATE fact_13f_yahoo_identifier_enrichment
                    SET applied = true, applied_at = now(), updated_at = now()
                    WHERE cusip = %s AND yahoo_symbol = %s
                    """,
                    (evidence.cusip, evidence.yahoo_symbol or ""),
                )
    return applied


def apply_accepted_evidence(limit: int | None = None) -> dict[str, int]:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when provided")
    limit_sql = "LIMIT %s" if limit is not None else ""
    apply_params: tuple[Any, ...] = (SOURCE_NAME, SOURCE_NAME) if limit is None else (limit, SOURCE_NAME, SOURCE_NAME)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fact_13f_yahoo_identifier_enrichment WHERE status = 'accepted'")
        accepted_total = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM fact_13f_yahoo_identifier_enrichment WHERE status = 'accepted' AND applied = true")
        already_applied = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(*)
            FROM fact_13f_yahoo_identifier_enrichment e
            JOIN dim_13f_security_us d ON d.cusip = e.cusip
            WHERE e.status = 'accepted'
              AND e.applied = false
              AND e.discovered_isin IS NOT NULL
              AND e.discovered_ticker IS NOT NULL
              AND substring(e.discovered_isin from 3 for 9) = e.cusip
              AND {candidate_where}
            """.format(candidate_where=candidate_where_sql("d"))
        )
        eligible_unapplied = int(cur.fetchone()[0])
        cur.execute(
            f"""
            WITH eligible AS (
                SELECT DISTINCT ON (e.cusip)
                       e.cusip, e.yahoo_symbol, e.discovered_ticker, e.discovered_isin,
                       e.search_query, e.query_strategy, e.confidence_score, e.status_reason,
                       e.yahoo_short_name, e.yahoo_long_name, e.yahoo_exchange, e.yahoo_quote_type,
                       e.sector, e.industry_group, e.updated_at
                FROM fact_13f_yahoo_identifier_enrichment e
                JOIN dim_13f_security_us d ON d.cusip = e.cusip
                WHERE e.status = 'accepted'
                  AND e.applied = false
                  AND e.discovered_isin IS NOT NULL
                  AND e.discovered_ticker IS NOT NULL
                  AND substring(e.discovered_isin from 3 for 9) = e.cusip
                  AND {candidate_where_sql("d")}
                ORDER BY e.cusip, e.confidence_score DESC, e.updated_at DESC, e.yahoo_symbol
                {limit_sql}
            ),
            updated_dim AS (
                UPDATE dim_13f_security_us d
                SET isin = e.discovered_isin,
                    primary_ticker = e.discovered_ticker,
                    resolution_status = 'resolved',
                    source_name = %s,
                    confidence_score = e.confidence_score,
                    sector = COALESCE(e.sector, d.sector),
                    industry_group = COALESCE(e.industry_group, d.industry_group),
                    evidence_payload = COALESCE(d.evidence_payload, '{{}}'::jsonb)
                        || jsonb_build_object('yahoo_finance', jsonb_build_object('source_name', %s, 'yahoo_symbol', e.yahoo_symbol, 'discovered_ticker', e.discovered_ticker, 'discovered_isin', e.discovered_isin, 'search_query', e.search_query, 'query_strategy', e.query_strategy, 'confidence_score', e.confidence_score, 'status_reason', e.status_reason, 'yahoo_short_name', e.yahoo_short_name, 'yahoo_long_name', e.yahoo_long_name, 'yahoo_exchange', e.yahoo_exchange, 'yahoo_quote_type', e.yahoo_quote_type)),
                    updated_at = now()
                FROM eligible e
                WHERE d.cusip = e.cusip AND d.isin IS NULL
                RETURNING e.cusip, e.yahoo_symbol
            ),
            marked_evidence AS (
                UPDATE fact_13f_yahoo_identifier_enrichment e
                SET applied = true, applied_at = now(), updated_at = now()
                FROM updated_dim u
                WHERE e.cusip = u.cusip AND e.yahoo_symbol = u.yahoo_symbol
                RETURNING e.cusip
            )
            SELECT COUNT(*) FROM marked_evidence
            """,
            apply_params,
        )
        applied = int(cur.fetchone()[0])
    return {"accepted_total": accepted_total, "already_applied": already_applied, "eligible_unapplied": eligible_unapplied, "applied": applied}


def _download_frames_by_symbol(symbols: list[str], period: str | None = None, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Run project dependency setup first.") from exc
    from xbrl_sec.sec.sources.yfinance_ingest import _configure_yfinance_cache

    _configure_yfinance_cache(yf)
    kwargs: dict[str, Any] = {
        "auto_adjust": False,
        "progress": False,
        "threads": True,
        "group_by": "ticker",
    }
    if period is not None:
        kwargs["period"] = period
    else:
        kwargs["start"] = start
        kwargs["end"] = end
    raw = yf.download(symbols, **kwargs)
    if raw is None or raw.empty:
        return {}

    frames: dict[str, Any] = {}
    single = len(symbols) == 1
    for symbol in symbols:
        try:
            if single:
                df = raw.copy()
            elif hasattr(raw, "columns") and hasattr(raw.columns, "get_level_values") and symbol in raw.columns.get_level_values(0):
                df = raw[symbol].copy()
            else:
                continue
        except (KeyError, TypeError):
            continue
        if df is not None and not df.empty and "Close" in df.columns:
            frames[symbol] = df
    return frames


def _frame_has_price_data(df: Any) -> bool:
    if df is None or df.empty or "Close" not in df.columns:
        return False
    close = df["Close"].dropna()
    return not close.empty


def _date_to_unix(value: date) -> int:
    return int(datetime.combine(value, datetime_time.min, tzinfo=timezone.utc).timestamp())


def _fetch_yahoo_chart(symbol: str, period: str | None = None, start: date | None = None, end: date | None = None) -> dict[str, Any] | None:
    encoded = quote(symbol, safe="")
    if period is not None:
        url = _YAHOO_CHART_RANGE_URL.format(symbol=encoded, range=quote(period, safe=""))
    else:
        if start is None:
            raise ValueError("start is required when period is not provided")
        end_exclusive = (end or date.today()) + timedelta(days=1)
        url = _YAHOO_CHART_PERIOD_URL.format(
            symbol=encoded,
            period1=_date_to_unix(start),
            period2=_date_to_unix(end_exclusive),
        )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(_read_url_with_retries(req, timeout=15).decode("utf-8", errors="ignore"))
    results = ((data.get("chart") or {}).get("result") or [])
    return results[0] if results else None


def _chart_has_price_data(symbol: str, period: str = "5d") -> bool:
    result = _fetch_yahoo_chart(symbol, period=period)
    if not result:
        return False
    quotes = ((result.get("indicators") or {}).get("quote") or [])
    if not quotes:
        return False
    closes = quotes[0].get("close") or []
    return any(value is not None for value in closes)


def _chart_daily_rows(symbol: str, start: date, end: date | None = None) -> tuple[str | None, list[tuple[date, float, float | None, int | None]]]:
    result = _fetch_yahoo_chart(symbol, start=start, end=end)
    if not result:
        return None, []
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    if not timestamps or not quotes:
        return (result.get("meta") or {}).get("currency"), []
    quote_row = quotes[0]
    closes = quote_row.get("close") or []
    volumes = quote_row.get("volume") or []
    adj_rows = indicators.get("adjclose") or []
    adj_closes = adj_rows[0].get("adjclose") if adj_rows else []
    currency = (result.get("meta") or {}).get("currency")
    rows: list[tuple[date, float, float | None, int | None]] = []
    for idx, ts in enumerate(timestamps):
        close_val = closes[idx] if idx < len(closes) else None
        if close_val is None:
            continue
        adj_val = adj_closes[idx] if adj_closes and idx < len(adj_closes) else close_val
        volume_val = volumes[idx] if idx < len(volumes) else None
        rows.append((
            datetime.fromtimestamp(int(ts), tz=timezone.utc).date(),
            float(close_val),
            float(adj_val) if adj_val is not None else None,
            int(volume_val) if volume_val is not None else None,
        ))
    return currency, rows


def _best_price_evidence(limit: int | None = None, only_unapplied: bool = True) -> list[dict[str, Any]]:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    limit_sql = "LIMIT %s" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    applied_filter = (
        """
                  AND e.applied = false
                  AND NOT EXISTS (
                      SELECT 1
                      FROM fact_13f_yahoo_identifier_enrichment prior
                      WHERE prior.cusip = e.cusip
                        AND prior.applied = true
                  )
        """
        if only_unapplied
        else ""
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH ranked AS (
                SELECT DISTINCT ON (e.cusip)
                       e.cusip,
                       e.yahoo_symbol,
                       COALESCE(NULLIF(e.discovered_ticker, ''), NULLIF(e.yahoo_symbol, '')) AS ticker,
                       e.discovered_isin,
                       e.status,
                       e.confidence_score,
                       e.search_query,
                       e.query_strategy,
                       e.status_reason,
                       e.yahoo_short_name,
                       e.yahoo_long_name,
                       e.yahoo_exchange,
                       e.yahoo_quote_type,
                       e.sector,
                       e.industry_group
                FROM fact_13f_yahoo_identifier_enrichment e
                JOIN dim_13f_security_us d ON d.cusip = e.cusip
                WHERE e.status IN ('accepted', 'ticker_only')
                  {applied_filter}
                  AND COALESCE(NULLIF(e.discovered_ticker, ''), NULLIF(e.yahoo_symbol, '')) IS NOT NULL
                  AND (
                      e.status = 'ticker_only'
                      OR (
                          e.status = 'accepted'
                          AND e.discovered_isin IS NOT NULL
                          AND substring(e.discovered_isin from 3 for 9) = e.cusip
                      )
                  )
                  AND d.security_title NOT IN ('CALL', 'PUT', 'WRNT', 'WTS', 'OPTION')
                  AND d.asset_bucket <> 'fixed_income'
                ORDER BY e.cusip,
                         CASE e.status WHEN 'accepted' THEN 0 ELSE 1 END,
                         e.confidence_score DESC,
                         e.updated_at DESC,
                         e.yahoo_symbol
            )
            SELECT *
            FROM ranked
            ORDER BY CASE status WHEN 'accepted' THEN 0 ELSE 1 END, confidence_score DESC, cusip
            {limit_sql}
            """,
            params,
        )
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _price_covered_evidence(candidates: list[dict[str, Any]], batch_size: int = 100, period: str = "5d") -> tuple[list[dict[str, Any]], int]:
    covered_symbols: set[str] = set()
    symbols = sorted({str(row["ticker"]).strip() for row in candidates if row.get("ticker")})
    workers = max(1, min(batch_size, 8))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_symbol = {executor.submit(_chart_has_price_data, symbol, period): symbol for symbol in symbols}
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                if future.result():
                    covered_symbols.add(symbol)
            except Exception:
                continue
    covered_rows = [row for row in candidates if row.get("ticker") in covered_symbols]
    return covered_rows, len(symbols) - len(covered_symbols)


def promote_price_covered_evidence(limit: int | None = None, batch_size: int = 100, period: str = "5d") -> dict[str, int]:
    from psycopg2.extras import execute_values
    from xbrl_sec.sec.db.connection import connect

    candidates = _best_price_evidence(limit=limit, only_unapplied=True)
    covered, no_price_symbols = _price_covered_evidence(candidates, batch_size=batch_size, period=period)
    if not covered:
        return {
            "candidates": len(candidates),
            "price_covered": 0,
            "promoted": 0,
            "accepted_promoted": 0,
            "ticker_only_promoted": 0,
            "symbols_without_price": no_price_symbols,
        }

    rows = [
        (
            row["cusip"],
            row["yahoo_symbol"] or "",
            row["ticker"],
            row["status"],
            row["discovered_isin"],
            row["confidence_score"],
            row["search_query"],
            row["query_strategy"],
            row["status_reason"],
            row["yahoo_short_name"],
            row["yahoo_long_name"],
            row["yahoo_exchange"],
            row["yahoo_quote_type"],
            row["sector"],
            row["industry_group"],
        )
        for row in covered
    ]

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE tmp_13f_price_promotion (
                cusip TEXT,
                yahoo_symbol TEXT,
                ticker TEXT,
                status TEXT,
                discovered_isin TEXT,
                confidence_score NUMERIC,
                search_query TEXT,
                query_strategy TEXT,
                status_reason TEXT,
                yahoo_short_name TEXT,
                yahoo_long_name TEXT,
                yahoo_exchange TEXT,
                yahoo_quote_type TEXT,
                sector TEXT,
                industry_group TEXT
            ) ON COMMIT DROP
            """
        )
        execute_values(
            cur,
            """
            INSERT INTO tmp_13f_price_promotion
                (cusip, yahoo_symbol, ticker, status, discovered_isin, confidence_score,
                 search_query, query_strategy, status_reason, yahoo_short_name, yahoo_long_name,
                 yahoo_exchange, yahoo_quote_type, sector, industry_group)
            VALUES %s
            """,
            rows,
            page_size=5000,
        )
        cur.execute(
            """
            WITH updated_dim AS (
                UPDATE dim_13f_security_us d
                SET isin = CASE
                        WHEN p.status = 'accepted'
                         AND p.discovered_isin IS NOT NULL
                         AND substring(p.discovered_isin from 3 for 9) = p.cusip
                        THEN p.discovered_isin
                        ELSE d.isin
                    END,
                    primary_ticker = p.ticker,
                    resolution_status = CASE WHEN p.status = 'accepted' THEN 'resolved' ELSE 'price_resolved' END,
                    source_name = %s,
                    confidence_score = p.confidence_score,
                    sector = COALESCE(p.sector, d.sector),
                    industry_group = COALESCE(p.industry_group, d.industry_group),
                    evidence_payload = COALESCE(d.evidence_payload, '{}'::jsonb)
                        || jsonb_build_object(
                            'yahoo_finance_price',
                            jsonb_build_object(
                                'source_name', %s,
                                'yahoo_symbol', p.yahoo_symbol,
                                'ticker', p.ticker,
                                'status', p.status,
                                'discovered_isin', p.discovered_isin,
                                'search_query', p.search_query,
                                'query_strategy', p.query_strategy,
                                'confidence_score', p.confidence_score,
                                'status_reason', p.status_reason,
                                'yahoo_short_name', p.yahoo_short_name,
                                'yahoo_long_name', p.yahoo_long_name,
                                'yahoo_exchange', p.yahoo_exchange,
                                'yahoo_quote_type', p.yahoo_quote_type
                            )
                        ),
                    updated_at = now()
                FROM tmp_13f_price_promotion p
                WHERE d.cusip = p.cusip
                RETURNING p.cusip, p.yahoo_symbol, p.status
            ),
            marked_evidence AS (
                UPDATE fact_13f_yahoo_identifier_enrichment e
                SET applied = true,
                    applied_at = now(),
                    updated_at = now()
                FROM updated_dim u
                WHERE e.cusip = u.cusip
                  AND e.yahoo_symbol = u.yahoo_symbol
                RETURNING e.status
            )
            SELECT status, COUNT(*)
            FROM marked_evidence
            GROUP BY status
            """,
            (PRICE_SOURCE_NAME, PRICE_SOURCE_NAME),
        )
        promoted_counts = dict(cur.fetchall())

    accepted_promoted = int(promoted_counts.get("accepted", 0))
    ticker_promoted = int(promoted_counts.get("ticker_only", 0))
    return {
        "candidates": len(candidates),
        "price_covered": len(covered),
        "promoted": accepted_promoted + ticker_promoted,
        "accepted_promoted": accepted_promoted,
        "ticker_only_promoted": ticker_promoted,
        "symbols_without_price": no_price_symbols,
    }


def _latest_13f_price_dates() -> dict[tuple[str, str], date]:
    from xbrl_sec.sec.db.connection import connect

    ensure_13f_price_table()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cusip, ticker, MAX(date) FROM fact_13f_prices_yahoo GROUP BY cusip, ticker")
        return {(row[0], row[1]): row[2] for row in cur.fetchall()}


def _promoted_price_universe(limit: int | None = None) -> list[dict[str, str]]:
    from xbrl_sec.sec.db.connection import connect

    ensure_evidence_table()
    limit_sql = "LIMIT %s" if limit is not None else ""
    params: tuple[Any, ...] = (PRICE_SOURCE_NAME, limit) if limit is not None else (PRICE_SOURCE_NAME,)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (e.cusip)
                   e.cusip,
                   COALESCE(NULLIF(e.discovered_ticker, ''), NULLIF(e.yahoo_symbol, '')) AS ticker,
                   e.status
            FROM fact_13f_yahoo_identifier_enrichment e
            JOIN dim_13f_security_us d ON d.cusip = e.cusip
            WHERE e.applied = true
              AND e.status IN ('accepted', 'ticker_only')
              AND COALESCE(NULLIF(e.discovered_ticker, ''), NULLIF(e.yahoo_symbol, '')) IS NOT NULL
              AND d.primary_ticker = COALESCE(NULLIF(e.discovered_ticker, ''), NULLIF(e.yahoo_symbol, ''))
              AND d.source_name = %s
            ORDER BY e.cusip,
                     CASE e.status WHEN 'accepted' THEN 0 ELSE 1 END,
                     e.confidence_score DESC,
                     e.updated_at DESC
            {limit_sql}
            """,
            params,
        )
        return [{"cusip": row[0], "ticker": row[1], "status": row[2]} for row in cur.fetchall()]


def download_13f_yahoo_prices(
    start_date: str = "2000-01-01",
    end_date: str | None = None,
    incremental: bool = True,
    limit: int | None = None,
    batch_size: int = 100,
) -> dict[str, int]:
    from psycopg2.extras import execute_values

    ensure_13f_price_table()
    universe = _promoted_price_universe(limit=limit)
    if not universe:
        return {"securities": 0, "tickers": 0, "rows": 0}

    latest = _latest_13f_price_dates() if incremental else {}
    default_start = date.fromisoformat(start_date)
    by_ticker: dict[str, list[dict[str, str]]] = {}
    for row in universe:
        by_ticker.setdefault(row["ticker"], []).append(row)

    total_rows = 0
    end_dt = date.fromisoformat(end_date) if end_date else date.today()
    tickers = sorted(by_ticker)
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        ticker_starts: dict[str, date] = {}
        for ticker in batch:
            starts = []
            for security in by_ticker[ticker]:
                prev = latest.get((security["cusip"], ticker))
                starts.append(prev + timedelta(days=1) if prev else default_start)
            ticker_starts[ticker] = min(starts)
        price_rows: list[tuple[Any, ...]] = []
        chart_results: dict[str, tuple[str | None, list[tuple[date, float, float | None, int | None]]]] = {}
        workers = max(1, min(batch_size, 8))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_ticker = {
                executor.submit(_chart_daily_rows, ticker, ticker_starts[ticker], end_dt): ticker
                for ticker in batch
            }
            for future in as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    chart_results[ticker] = future.result()
                except Exception:
                    chart_results[ticker] = (None, [])
        for ticker, (currency, daily_rows) in chart_results.items():
            if not daily_rows:
                continue
            for idx, (d, close_val, adj_val, vol_val) in enumerate(daily_rows):
                if d < ticker_starts[ticker]:
                    continue
                if math.isnan(close_val):
                    continue
                if idx > 0:
                    prev_adj = daily_rows[idx - 1][2]
                    if prev_adj is not None and adj_val is not None and not math.isnan(prev_adj) and prev_adj != 0 and not math.isnan(adj_val):
                        ret = (adj_val - prev_adj) / prev_adj
                        log_ret = math.log(adj_val / prev_adj) if adj_val > 0 and prev_adj > 0 else None
                        abs_diff = adj_val - prev_adj
                    else:
                        ret = log_ret = abs_diff = None
                else:
                    ret = log_ret = abs_diff = None
                for security in by_ticker[ticker]:
                    price_rows.append((
                        d,
                        security["cusip"],
                        ticker,
                        close_val,
                        ret,
                        log_ret,
                        abs_diff,
                        currency,
                        "US",
                        adj_val if adj_val is not None and not math.isnan(adj_val) else None,
                        vol_val,
                        None,
                        security["status"],
                        "yahoo_chart",
                    ))
        if not price_rows:
            continue
        from xbrl_sec.sec.db.connection import connect

        with connect() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO fact_13f_prices_yahoo
                    (date, cusip, ticker, close, return, log_return, abs_diff, currency,
                     jurisdiction, adj_close, volume, shares_outstanding, identifier_status, source_name)
                VALUES %s
                ON CONFLICT (cusip, ticker, date) DO UPDATE SET
                    close = EXCLUDED.close,
                    return = EXCLUDED.return,
                    log_return = EXCLUDED.log_return,
                    abs_diff = EXCLUDED.abs_diff,
                    currency = EXCLUDED.currency,
                    jurisdiction = EXCLUDED.jurisdiction,
                    adj_close = EXCLUDED.adj_close,
                    volume = EXCLUDED.volume,
                    shares_outstanding = EXCLUDED.shares_outstanding,
                    identifier_status = EXCLUDED.identifier_status,
                    source_name = EXCLUDED.source_name,
                    updated_at = now()
                """,
                price_rows,
                page_size=5000,
            )
        total_rows += len(price_rows)
    return {"securities": len(universe), "tickers": len(tickers), "rows": total_rows}


def search_quote_candidates_api_first(candidate: SecurityCandidate, max_symbols_per_query: int = 3) -> list[YahooQuoteCandidate]:
    found: dict[str, YahooQuoteCandidate] = {}
    query_errors: list[BaseException] = []
    for strategy, query in build_search_queries(candidate):
        for searcher in ("businessinsider", "yahoo"):
            try:
                if searcher == "businessinsider":
                    quote_candidates = _search_business_insider_suggest(query, strategy, max_symbols=max_symbols_per_query, target_cusip=candidate.cusip)
                else:
                    quote_candidates = _search_yahoo_suggest(query, strategy, max_symbols=max_symbols_per_query)
            except Exception as exc:
                query_errors.append(exc)
                continue
            for quote_candidate in quote_candidates:
                if quote_candidate.symbol not in found:
                    found[quote_candidate.symbol] = quote_candidate
    if query_errors and not found:
        raise query_errors[0]
    return list(found.values())


def enrich_candidate_api_first(candidate: SecurityCandidate, max_symbols_per_query: int = 3, driver: Any | None = None, wait_seconds: float = 6.0, selenium_fallback: bool = False) -> list[EnrichmentEvidence]:
    evidence_rows: list[EnrichmentEvidence] = []
    seen_symbols: set[str] = set()
    acquisition_error: BaseException | None = None
    try:
        quote_candidates = search_quote_candidates_api_first(candidate, max_symbols_per_query=max_symbols_per_query)
    except Exception as exc:
        quote_candidates = []
        acquisition_error = exc

    if selenium_fallback and driver is not None:
        for strategy, query in build_search_queries(candidate):
            try:
                fallback_candidates = search_yahoo_quotes(driver, query, strategy, max_symbols=max_symbols_per_query, wait_seconds=wait_seconds)
            except Exception as exc:
                if acquisition_error is None:
                    acquisition_error = exc
                continue
            existing_symbols = {quote_candidate.symbol for quote_candidate in quote_candidates}
            for fallback_candidate in fallback_candidates:
                if fallback_candidate.symbol not in existing_symbols:
                    quote_candidates.append(fallback_candidate)
                    existing_symbols.add(fallback_candidate.symbol)

    for yahoo_candidate in quote_candidates:
        if yahoo_candidate.symbol in seen_symbols:
            continue
        seen_symbols.add(yahoo_candidate.symbol)
        known_isin, known_isin_source = _known_isin_from_quote_candidate(yahoo_candidate)
        try:
            if not quote_candidate_relevant_to_candidate(candidate, yahoo_candidate):
                continue
            if known_isin and isin_matches_cusip(known_isin, candidate.cusip):
                metadata = _metadata_from_quote_candidate_payload(yahoo_candidate)
            else:
                metadata = fetch_yfinance_metadata(yahoo_candidate.symbol, candidate.cusip, known_isin=known_isin, known_isin_source=known_isin_source)
            if not metadata_relevant_to_candidate(candidate, yahoo_candidate.symbol, metadata):
                continue
            evidence = build_evidence(candidate, yahoo_candidate, metadata)
        except Exception as exc:
            evidence = build_evidence(candidate, yahoo_candidate, None, exc)
        evidence_rows.append(evidence)
        if evidence.status == "accepted":
            return evidence_rows

    if not evidence_rows:
        if acquisition_error is not None and not quote_candidates:
            evidence_rows.append(build_evidence(candidate, None, None, acquisition_error))
        else:
            evidence_rows.append(build_evidence(candidate, None, None))
    return evidence_rows


def _dismiss_yahoo_consent(driver: Any) -> None:
    try:
        from selenium.webdriver.common.by import By
    except ImportError:
        return
    xpaths = [
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept all')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'reject all')]",
    ]
    for xpath in xpaths:
        try:
            for button in driver.find_elements(By.XPATH, xpath)[:1]:
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    time.sleep(0.5)
                    return
        except Exception:
            continue


def _extract_quote_candidates(driver: Any, search_query: str, query_strategy: str, max_symbols: int) -> list[YahooQuoteCandidate]:
    from selenium.webdriver.common.by import By

    found: dict[str, dict[str, Any]] = {}
    query_cusip = normalize_cusip(search_query)
    current_symbol = _symbol_from_url(getattr(driver, "current_url", "") or "")
    if current_symbol and current_symbol != query_cusip:
        found[current_symbol] = {"source": "current_url", "href": getattr(driver, "current_url", ""), "text": ""}
    for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href*='/quote/']"):
        try:
            href = anchor.get_attribute("href") or ""
            symbol = _symbol_from_url(href)
            if symbol is None or symbol == query_cusip or symbol in found:
                continue
            found[symbol] = {"source": "anchor", "href": href, "text": (anchor.text or "").strip()}
            if len(found) >= max_symbols:
                break
        except Exception:
            continue
    return [YahooQuoteCandidate(symbol, search_query, query_strategy, rank, payload) for rank, (symbol, payload) in enumerate(found.items(), start=1)]


def _find_yahoo_search_input(driver: Any) -> Any | None:
    from selenium.webdriver.common.by import By

    selectors = ["input[name='yfin-usr-qry']", "input#ybar-sbq", "input[aria-label*='Search']", "input[placeholder*='Search']", "input[type='search']"]
    for selector in selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    return element
        except Exception:
            continue
    return None


def _extract_autocomplete_candidates(driver: Any, search_query: str, query_strategy: str, max_symbols: int, wait_seconds: float) -> list[YahooQuoteCandidate]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get("https://finance.yahoo.com/")
    _dismiss_yahoo_consent(driver)
    try:
        WebDriverWait(driver, wait_seconds).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception:
        pass
    search_input = _find_yahoo_search_input(driver)
    if search_input is None:
        return []
    search_input.click()
    search_input.send_keys(Keys.CONTROL, "a")
    search_input.send_keys(search_query)
    time.sleep(min(max(wait_seconds / 3, 0.75), 2.0))

    found: dict[str, YahooQuoteCandidate] = {}
    for candidate in _extract_quote_candidates(driver, search_query, query_strategy, max_symbols):
        found[candidate.symbol] = candidate
    query_cusip = normalize_cusip(search_query)
    for selector in ["[role='listbox'] a", "[role='option']", "li[role='option']", "[data-testid*='suggest'] a", "[data-testid*='search'] a"]:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                text = (element.text or "").strip()
                symbol = _symbol_from_autocomplete_text(text)
                if symbol is None or symbol == query_cusip or symbol in found:
                    continue
                found[symbol] = YahooQuoteCandidate(symbol, search_query, query_strategy, len(found) + 1, {"source": "autocomplete", "text": text})
                if len(found) >= max_symbols:
                    return list(found.values())[:max_symbols]
            except Exception:
                continue
    return list(found.values())[:max_symbols]


def search_yahoo_quotes(driver: Any, search_query: str, query_strategy: str, max_symbols: int = 3, wait_seconds: float = 6.0) -> list[YahooQuoteCandidate]:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    seen_symbols: dict[str, YahooQuoteCandidate] = {}
    try:
        for candidate in _search_yahoo_suggest(search_query, query_strategy, max_symbols):
            seen_symbols.setdefault(candidate.symbol, candidate)
    except Exception:
        pass
    if len(seen_symbols) >= max_symbols:
        return list(seen_symbols.values())[:max_symbols]
    for candidate in _extract_autocomplete_candidates(driver, search_query, query_strategy, max_symbols=max_symbols, wait_seconds=wait_seconds):
        seen_symbols.setdefault(candidate.symbol, candidate)
    if len(seen_symbols) >= max_symbols:
        return list(seen_symbols.values())[:max_symbols]
    encoded = quote(search_query, safe="")
    for url in [f"https://finance.yahoo.com/quote/{encoded}", f"https://finance.yahoo.com/lookup?s={encoded}"]:
        driver.get(url)
        _dismiss_yahoo_consent(driver)
        try:
            WebDriverWait(driver, wait_seconds).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except Exception:
            pass
        for candidate in _extract_quote_candidates(driver, search_query, query_strategy, max_symbols):
            seen_symbols.setdefault(candidate.symbol, candidate)
        if len(seen_symbols) >= max_symbols:
            break
    return list(seen_symbols.values())[:max_symbols]


def create_edge_driver(headless: bool = True, edge_binary_path: str = DEFAULT_EDGE_BINARY_PATH, driver_path: str | None = None) -> Any:
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service
    except ImportError as exc:
        raise RuntimeError("selenium is not installed. Install it into project-local deps with `pip install --target .python_deps selenium`.") from exc
    options = Options()
    if Path(edge_binary_path).exists():
        options.binary_location = edge_binary_path
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1400,1000")
    service = Service(executable_path=driver_path) if driver_path else Service()
    return webdriver.Edge(service=service, options=options)


def enrich_candidate(candidate: SecurityCandidate, driver: Any, max_symbols_per_query: int = 3, wait_seconds: float = 6.0) -> list[EnrichmentEvidence]:
    return enrich_candidate_api_first(candidate, max_symbols_per_query=max_symbols_per_query, driver=driver, wait_seconds=wait_seconds, selenium_fallback=True)


def _should_retry_with_new_driver(rows: list[EnrichmentEvidence]) -> bool:
    if len(rows) != 1 or rows[0].status != "error":
        return False
    text = f"{rows[0].error_type or ''} {rows[0].error_message or ''}".lower()
    return any(token in text for token in ("maxretryerror", "connection refused", "connection aborted", "invalid session id", "disconnected"))


def run_enrichment(limit: int | None = None, apply: bool = False, resume: bool = False, headless: bool = True, sleep_seconds: float = 0.5, edge_binary_path: str = DEFAULT_EDGE_BINARY_PATH, driver_path: str | None = None, max_symbols_per_query: int = 3, workers: int = 4, selenium_fallback: bool = False) -> dict[str, int]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    candidates = select_candidates(limit=limit, resume=resume, apply=apply)
    counts = {"candidates": len(candidates), "evidence_rows": 0, "applied": 0, "accepted": 0, "ticker_only": 0, "conflict": 0, "not_found": 0, "error": 0}
    if not candidates:
        return counts

    def record_rows(rows: list[EnrichmentEvidence]) -> None:
        for evidence in rows:
            counts["evidence_rows"] += 1
            counts[evidence.status] += 1
            counts["applied"] += upsert_evidence(evidence, apply=apply)

    if selenium_fallback:
        resolved_driver_path = driver_path or os.environ.get("YAHOO_FINANCE_EDGE_DRIVER")
        driver = create_edge_driver(headless=headless, edge_binary_path=edge_binary_path, driver_path=resolved_driver_path)
        try:
            for idx, candidate in enumerate(candidates, start=1):
                rows = enrich_candidate_api_first(candidate, max_symbols_per_query=max_symbols_per_query, driver=driver, selenium_fallback=True)
                if _should_retry_with_new_driver(rows):
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_edge_driver(headless=headless, edge_binary_path=edge_binary_path, driver_path=resolved_driver_path)
                    rows = enrich_candidate_api_first(candidate, max_symbols_per_query=max_symbols_per_query, driver=driver, selenium_fallback=True)
                record_rows(rows)
                if sleep_seconds > 0 and idx < len(candidates):
                    time.sleep(sleep_seconds)
        finally:
            driver.quit()
        return counts

    if workers == 1:
        for idx, candidate in enumerate(candidates, start=1):
            record_rows(enrich_candidate_api_first(candidate, max_symbols_per_query=max_symbols_per_query))
            if sleep_seconds > 0 and idx < len(candidates):
                time.sleep(sleep_seconds)
        return counts

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_candidate = {executor.submit(enrich_candidate_api_first, candidate, max_symbols_per_query): candidate for candidate in candidates}
        for idx, future in enumerate(as_completed(future_to_candidate), start=1):
            candidate = future_to_candidate[future]
            try:
                rows = future.result()
            except Exception as exc:
                rows = [build_evidence(candidate, None, None, exc)]
            record_rows(rows)
            if sleep_seconds > 0 and idx < len(candidates):
                time.sleep(sleep_seconds)
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Enrich 13F CUSIPs with Yahoo Finance symbols and yfinance ISINs.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--edge-binary-path", default=DEFAULT_EDGE_BINARY_PATH)
    parser.add_argument("--driver-path", default=None)
    parser.add_argument("--max-symbols-per-query", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--selenium-fallback", action="store_true")
    args = parser.parse_args(argv)
    result = run_enrichment(limit=args.limit, apply=args.apply, resume=args.resume, headless=args.headless, sleep_seconds=args.sleep_seconds, edge_binary_path=args.edge_binary_path, driver_path=args.driver_path, max_symbols_per_query=args.max_symbols_per_query, workers=args.workers, selenium_fallback=args.selenium_fallback)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
