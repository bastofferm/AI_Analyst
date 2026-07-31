"""Daily OHLCV ingestion for ETFs via yfinance (WA0006 §2.2, §4.2 Stage 3).

yfinance accepts ISIN directly for most UCITS ETFs; falls back to an
exchange-ticker + MIC suffix (.DE Xetra / .VI Vienna) when the ISIN returns
nothing. Writes sec.fact_prices_etf and updates sec.pipeline_etf_state.
"""
from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.db.connection import connect
from .models import PriceRecord
from .price_sources import (
    LicensedJustEtfPriceAdapter,
    PriceSourceCandidate,
    select_best_price_candidate,
    upsert_price_source_candidates,
)
from .writers import upsert_prices, set_etf_price_state

# MIC -> yfinance suffix for the ISIN fallback path.
_MIC_SUFFIX = {
    "XETR": ".DE",
    "GETT": ".DE",
    "XFRA": ".DE",
    "XSTU": ".DE",
    "XMUN": ".DE",
    "XDUS": ".DE",
    "XHAM": ".DE",
    "XHAN": ".DE",
    "XBER": ".DE",
    "TGAT": ".DE",
    "XWBO": ".VI",
}
_SYMBOL_SUFFIX_TO_MIC = {
    ".DE": "XETR",
    ".F": "XFRA",
    ".SG": "XSTU",
    ".DU": "XDUS",
    ".MU": "XMUN",
    ".HM": "XHAM",
    ".VI": "XWBO",
}
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_HISTORY_PERIOD_FALLBACKS = ("max", "10y", "5y", "2y", "1y", "6mo", "1mo", "5d")


def _configure_cache(yf) -> None:
    cache_dir = load_settings().project_root / ".cache" / "yfinance"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(str(cache_dir))


def _history_period_attempts(period: str) -> list[str]:
    requested = str(period or "max").strip() or "max"
    out = [requested]
    for fallback in _HISTORY_PERIOD_FALLBACKS:
        if fallback not in out:
            out.append(fallback)
    return out


def _history_with_period_fallback(ticker, period: str):
    """Return the longest non-empty Yahoo history available for a requested period."""
    last_empty = None
    for attempt in _history_period_attempts(period):
        try:
            df = ticker.history(period=attempt, auto_adjust=True)
        except Exception:
            continue
        if df is not None and not df.empty:
            return df
        last_empty = df
    return last_empty


def _rows_from_history(
    df,
    isin: str,
    mic: str,
    currency: str | None,
    *,
    source: str = "yfinance",
    history_kind: str = "market_price",
    source_symbol: str | None = None,
) -> list[PriceRecord]:
    if df is None or df.empty or "Close" not in df.columns:
        return []
    df = df.dropna(subset=["Close"])
    out: list[PriceRecord] = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        close_v = float(row["Close"])
        if math.isnan(close_v):
            continue

        def _f(col: str) -> float | None:
            if col not in df.columns:
                return None
            v = row[col]
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            return None if math.isnan(v) else v

        vol = _f("Volume")
        out.append(PriceRecord(
            isin=isin, mic=mic, price_date=d, close=close_v,
            open=_f("Open"), high=_f("High"), low=_f("Low"),
            volume=int(vol) if vol is not None else None, currency=currency,
            source=source, history_kind=history_kind, source_symbol=source_symbol,
        ))
    return out


def _resolved_yahoo_symbols(isins: list[str]) -> dict[str, str]:
    if not isins:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT isin, yf_ticker
            FROM sec.dim_etf_profile
            WHERE isin = ANY(%s)
              AND yf_ticker IS NOT NULL
              AND BTRIM(yf_ticker) <> ''
            """,
            (isins,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _listing_exchange_tickers(isins: list[str]) -> dict[str, list[tuple[str, str, str | None]]]:
    if not isins:
        return {}
    sql = """
        SELECT d.isin, l.mic,
               COALESCE(NULLIF(BTRIM(l.exchange_ticker), ''), NULLIF(BTRIM(j.primary_ticker), '')) AS exchange_ticker,
               COALESCE(l.trading_currency, d.fund_currency, j.fund_currency) AS currency
        FROM sec.dim_etf d
        JOIN sec.dim_etf_listing l ON l.isin = d.isin
        LEFT JOIN sec.etf_justetf_profile j ON j.isin = d.isin
        WHERE d.isin = ANY(%s)
          AND COALESCE(NULLIF(BTRIM(l.exchange_ticker), ''), NULLIF(BTRIM(j.primary_ticker), '')) IS NOT NULL
    """
    out: dict[str, list[tuple[str, str, str | None]]] = {}
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (isins,))
            for isin, mic, ticker, currency in cur.fetchall():
                clean = _exchange_ticker_base(ticker)
                if clean:
                    out.setdefault(isin, []).append((mic, clean, currency))
    except Exception:
        return {}
    return out


def _is_unresolved_yahoo_symbol(symbol: str | None, isin: str) -> bool:
    text = str(symbol or "").strip().upper()
    return not text or text == isin.upper() or bool(_ISIN_RE.fullmatch(text))


def _ticker_attempts(isin: str, mic: str, resolved_symbol: str | None) -> list[str]:
    out: list[str] = []
    if not _is_unresolved_yahoo_symbol(resolved_symbol, isin):
        out.append(str(resolved_symbol).strip().upper())
    out.append(isin)
    if mic in _MIC_SUFFIX:
        out.append(f"{isin}{_MIC_SUFFIX[mic]}")
    deduped: list[str] = []
    for symbol in out:
        if symbol not in deduped:
            deduped.append(symbol)
    return deduped


def _exchange_ticker_base(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if not text or _ISIN_RE.fullmatch(text):
        return None
    text = re.sub(r"[^A-Z0-9.]+", "", text)
    if "." in text:
        text = text.split(".", 1)[0]
    return text or None


def _symbol_base(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    for suffix in _SYMBOL_SUFFIX_TO_MIC:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text.split(".", 1)[0]


def _symbol_mic(symbol: str, default_mic: str) -> str:
    text = symbol.upper()
    for suffix, mic in _SYMBOL_SUFFIX_TO_MIC.items():
        if text.endswith(suffix):
            return mic
    return default_mic


def _candidate_attempts(
    isin: str,
    mic: str,
    resolved_symbol: str | None,
    exchange_tickers: list[tuple[str, str, str | None]] | None,
) -> list[tuple[str, str, str]]:
    attempts: list[tuple[str, str, str]] = []
    if not _is_unresolved_yahoo_symbol(resolved_symbol, isin):
        symbol = str(resolved_symbol).strip().upper()
        attempts.append(("yfinance.promoted", symbol, _symbol_mic(symbol, mic)))

    for listing_mic, ticker, _currency in sorted(exchange_tickers or (), key=lambda row: (row[0] != "XETR", row[0])):
        suffix = _MIC_SUFFIX.get(listing_mic) or _MIC_SUFFIX.get(mic)
        if suffix:
            symbol = f"{ticker}{suffix}"
            attempts.append(("yfinance.exchange_ticker", symbol, _symbol_mic(symbol, listing_mic)))
        attempts.append(("yfinance.exchange_ticker", ticker, listing_mic))

    for symbol in _ticker_attempts(isin, mic, resolved_symbol):
        source = "yfinance.isin_suffix" if symbol != isin else "yfinance.isin"
        attempts.append((source, symbol, _symbol_mic(symbol, mic)))

    deduped: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for source, symbol, attempt_mic in attempts:
        key = symbol.upper()
        if key not in seen:
            seen.add(key)
            deduped.append((source, key, attempt_mic))
    return deduped


def _ticker_currency(ticker: Any) -> str | None:
    try:
        fast_info = getattr(ticker, "fast_info", None)
        if fast_info is None:
            return None
        currency = fast_info.get("currency") if hasattr(fast_info, "get") else getattr(fast_info, "currency", None)
        return str(currency).upper() if currency else None
    except Exception:
        return None


def _candidate_from_yahoo(yf: Any, isin: str, mic: str, symbol: str, source: str, period: str) -> PriceSourceCandidate:
    try:
        ticker = yf.Ticker(symbol)
        df = _history_with_period_fallback(ticker, period)
        currency = _ticker_currency(ticker)
        records = _rows_from_history(
            df,
            isin,
            mic,
            currency,
            source=source,
            history_kind="market_price",
            source_symbol=symbol,
        )
        if not records:
            return PriceSourceCandidate(
                isin=isin,
                source=source,
                symbol=symbol,
                mic=mic,
                status="empty",
                currency=currency,
                error="no price data",
                source_url=f"https://finance.yahoo.com/quote/{symbol}",
            )
        records.sort(key=lambda row: row.price_date)
        return PriceSourceCandidate(
            isin=isin,
            source=source,
            symbol=symbol,
            mic=mic,
            status="complete",
            records=tuple(records),
            first_price_date=records[0].price_date,
            last_price_date=records[-1].price_date,
            currency=currency,
            source_url=f"https://finance.yahoo.com/quote/{symbol}",
        )
    except Exception as exc:  # noqa: BLE001 - persisted as candidate evidence
        return PriceSourceCandidate(
            isin=isin,
            source=source,
            symbol=symbol,
            mic=mic,
            status="failed",
            error=str(exc)[:300],
            source_url=f"https://finance.yahoo.com/quote/{symbol}",
        )


def _promote_successful_price_symbol(isin: str, candidate: PriceSourceCandidate) -> None:
    if not candidate.source.startswith("yfinance."):
        return
    if _is_unresolved_yahoo_symbol(candidate.symbol, isin):
        return
    base = _symbol_base(candidate.symbol)
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sec.dim_etf_profile (isin, yf_ticker, profile_status, updated_at)
                VALUES (%s, %s, 'pending', NOW())
                ON CONFLICT (isin) DO UPDATE SET
                    yf_ticker = EXCLUDED.yf_ticker,
                    profile_status = CASE
                        WHEN sec.dim_etf_profile.profile_status IN ('pending', 'empty', 'failed')
                        THEN 'pending'
                        ELSE sec.dim_etf_profile.profile_status
                    END,
                    updated_at = NOW()
                """,
                (isin, candidate.symbol),
            )
            if base:
                cur.execute(
                    """
                    UPDATE sec.dim_etf_listing
                    SET exchange_ticker = %s
                    WHERE isin = %s
                      AND mic = %s
                      AND (exchange_ticker IS NULL OR BTRIM(exchange_ticker) = '')
                    """,
                    (base, isin, candidate.mic),
                )
    except Exception:
        return


def fetch_etf_prices(
    pairs: list[tuple[str, str | None]],
    period: str = "1y",
    *,
    use_fallbacks: bool = True,
    allow_licensed_justetf: bool = False,
    licensed_justetf_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Fetch and upsert prices for (isin, mic) pairs. Returns summary counts."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance") from exc
    _configure_cache(yf)

    total_rows = 0
    ok = 0
    empty = 0
    failed = 0
    candidate_rows = 0
    source_breakdown: dict[str, int] = {}
    isins = [isin for isin, _ in pairs]
    resolved_symbols = _resolved_yahoo_symbols(isins)
    listing_tickers = _listing_exchange_tickers(isins) if use_fallbacks else {}
    licensed_adapter = LicensedJustEtfPriceAdapter(licensed_justetf_dir)
    for isin, mic in pairs:
        mic = mic or "XETR"
        try:
            candidates: list[PriceSourceCandidate] = []
            for source, symbol, candidate_mic in _candidate_attempts(
                isin,
                mic,
                resolved_symbols.get(isin),
                listing_tickers.get(isin) if use_fallbacks else None,
            ):
                candidate = _candidate_from_yahoo(yf, isin, candidate_mic, symbol, source, period)
                candidates.append(candidate)
                if candidate.status == "complete" and not use_fallbacks:
                    break

            if use_fallbacks and allow_licensed_justetf:
                candidates.append(licensed_adapter.fetch(isin, mic))

            try:
                candidate_rows += upsert_price_source_candidates(candidates)
            except Exception:
                pass

            preferred_currencies = [currency for _mic, _ticker, currency in listing_tickers.get(isin, [])]
            best = select_best_price_candidate(candidates, preferred_currencies=preferred_currencies)
            if best is None:
                empty += 1
                attempted = ",".join(c.symbol for c in candidates)
                errors = "; ".join(f"{c.symbol}:{c.error or c.status}" for c in candidates if c.status != "complete")
                set_etf_price_state(isin, "failed", None, f"no price data for {attempted}; {errors}"[:300])
                continue
            n = upsert_prices(best.records)
            total_rows += n
            ok += 1
            source_breakdown[best.source] = source_breakdown.get(best.source, 0) + 1
            last_d: date = max(r.price_date for r in best.records)
            set_etf_price_state(isin, "complete", last_d, None)
            _promote_successful_price_symbol(isin, best)
        except Exception as exc:  # noqa: BLE001 - record and continue
            failed += 1
            set_etf_price_state(isin, "failed", None, str(exc)[:300])
    return {
        "requested": len(pairs),
        "ok": ok,
        "empty": empty,
        "failed": failed,
        "rows": total_rows,
        "candidate_rows": candidate_rows,
        "source_breakdown": source_breakdown,
    }
