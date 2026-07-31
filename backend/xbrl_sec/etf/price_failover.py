"""Multi-source price failover adapters for ETF daily ingestion.

Used inside the LangGraph prices_fetch node as a RunnableWithFallbacks chain:
Yahoo (primary, in prices.py) → Stooq (free CSV) → Polygon (licensed REST).

Each adapter returns a PriceSourceCandidate so the existing
select_best_price_candidate() logic decides whether the fallback wins on
history length / source priority. Network errors are captured as candidate
evidence — adapters never raise to the caller, so the LangGraph node can
record provenance and proceed.
"""
from __future__ import annotations

import csv
import io
import math
import os
from datetime import date
from typing import Any, Iterable

import httpx

from .models import PriceRecord
from .price_sources import PriceSourceCandidate


_STOOQ_BASE = "https://stooq.com/q/d/l/"
_POLYGON_BASE = "https://api.polygon.io"


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return None if math.isnan(result) else result


def _candidate_failed(isin: str, source: str, symbol: str, mic: str, message: str) -> PriceSourceCandidate:
    return PriceSourceCandidate(
        isin=isin,
        source=source,
        symbol=symbol,
        mic=mic,
        status="failed",
        error=message[:300],
    )


def _candidate_empty(isin: str, source: str, symbol: str, mic: str, message: str) -> PriceSourceCandidate:
    return PriceSourceCandidate(
        isin=isin,
        source=source,
        symbol=symbol,
        mic=mic,
        status="empty",
        error=message[:300],
    )


def _stooq_symbol(isin: str, listing_symbol: str | None, mic: str) -> str:
    """Stooq quote suffixes mirror Yahoo but with lowercase + .de / .uk."""
    base = (listing_symbol or isin).lower()
    if "." in base:
        return base
    if mic in {"XETR", "XFRA", "XSTU", "XMUN", "XDUS", "XHAM"}:
        return f"{base}.de"
    if mic == "XLON":
        return f"{base}.uk"
    return base


class StooqPriceAdapter:
    """Stooq free CSV adapter — used as the second tier when Yahoo fails."""

    source = "stooq.csv"

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(
        self,
        isin: str,
        mic: str,
        *,
        listing_symbol: str | None = None,
        period: str = "max",
        currency: str | None = None,
    ) -> PriceSourceCandidate:
        isin = isin.strip().upper()
        symbol = _stooq_symbol(isin, listing_symbol, mic)
        params = {"s": symbol, "i": "d"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(_STOOQ_BASE, params=params)
        except httpx.HTTPError as exc:
            return _candidate_failed(isin, self.source, symbol, mic, f"network: {exc}")
        if response.status_code != 200 or not response.text:
            return _candidate_failed(
                isin,
                self.source,
                symbol,
                mic,
                f"http {response.status_code}: {response.text[:120]}",
            )
        text = response.text.lstrip("﻿")
        if text.startswith("No data") or text.startswith("Brak danych"):
            return _candidate_empty(isin, self.source, symbol, mic, "stooq returned 'no data'")

        reader = csv.DictReader(io.StringIO(text))
        records: list[PriceRecord] = []
        for row in reader:
            day = _iso(str(row.get("Date") or row.get("date") or ""))
            close = _safe_float(row.get("Close") or row.get("close"))
            if day is None or close is None:
                continue
            records.append(
                PriceRecord(
                    isin=isin,
                    mic=mic,
                    price_date=day,
                    close=close,
                    open=_safe_float(row.get("Open") or row.get("open")),
                    high=_safe_float(row.get("High") or row.get("high")),
                    low=_safe_float(row.get("Low") or row.get("low")),
                    volume=int(_safe_float(row.get("Volume") or row.get("volume")) or 0) or None,
                    currency=currency,
                    source=self.source,
                    history_kind="market_price",
                    source_symbol=symbol,
                )
            )
        if not records:
            return _candidate_empty(isin, self.source, symbol, mic, "stooq CSV had no usable rows")
        records.sort(key=lambda r: r.price_date)
        return PriceSourceCandidate(
            isin=isin,
            source=self.source,
            symbol=symbol,
            mic=mic,
            history_kind="market_price",
            status="complete",
            records=tuple(records),
            first_price_date=records[0].price_date,
            last_price_date=records[-1].price_date,
            currency=currency,
            source_url=f"{_STOOQ_BASE}?s={symbol}&i=d",
        )


class PolygonPriceAdapter:
    """Polygon.io daily aggregates adapter — licensed third tier.

    Activated only when POLYGON_API_KEY is configured. Polygon does not index
    European MIC tickers cleanly; we use the explicit listing symbol when
    provided, otherwise the ISIN-derived fallback.
    """

    source = "polygon.aggs"

    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key or os.environ.get("POLYGON_API_KEY") or ""
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch(
        self,
        isin: str,
        mic: str,
        *,
        listing_symbol: str | None = None,
        period: str = "max",
        currency: str | None = None,
    ) -> PriceSourceCandidate:
        isin = isin.strip().upper()
        symbol = (listing_symbol or isin).upper()
        if not self.enabled:
            return PriceSourceCandidate(
                isin=isin,
                source=self.source,
                symbol=symbol,
                mic=mic,
                status="skipped",
                error="POLYGON_API_KEY not configured",
            )
        from_date = "2010-01-01"
        to_date = date.today().isoformat()
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{from_date}/{to_date}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    f"{_POLYGON_BASE}{path}",
                    params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self.api_key},
                )
        except httpx.HTTPError as exc:
            return _candidate_failed(isin, self.source, symbol, mic, f"network: {exc}")
        if response.status_code != 200:
            return _candidate_failed(
                isin,
                self.source,
                symbol,
                mic,
                f"http {response.status_code}: {response.text[:120].replace(self.api_key, '***')}",
            )
        data = response.json() or {}
        rows = data.get("results") or []
        if not rows:
            return _candidate_empty(isin, self.source, symbol, mic, "polygon returned no aggregates")
        records: list[PriceRecord] = []
        for row in rows:
            ts = row.get("t")
            close = row.get("c")
            if ts is None or close is None:
                continue
            day = date.fromtimestamp(int(ts) / 1000.0)
            records.append(
                PriceRecord(
                    isin=isin,
                    mic=mic,
                    price_date=day,
                    close=float(close),
                    open=_safe_float(row.get("o")),
                    high=_safe_float(row.get("h")),
                    low=_safe_float(row.get("l")),
                    volume=int(row.get("v")) if row.get("v") is not None else None,
                    currency=currency,
                    source=self.source,
                    history_kind="market_price",
                    source_symbol=symbol,
                )
            )
        if not records:
            return _candidate_empty(isin, self.source, symbol, mic, "polygon aggregates parsed empty")
        records.sort(key=lambda r: r.price_date)
        return PriceSourceCandidate(
            isin=isin,
            source=self.source,
            symbol=symbol,
            mic=mic,
            history_kind="market_price",
            status="complete",
            records=tuple(records),
            first_price_date=records[0].price_date,
            last_price_date=records[-1].price_date,
            currency=currency,
            source_url=f"https://polygon.io/quote/{symbol}",
        )


def failover_chain(
    isin: str,
    mic: str,
    *,
    listing_symbol: str | None = None,
    currency: str | None = None,
    period: str = "max",
    adapters: Iterable[Any] | None = None,
) -> list[PriceSourceCandidate]:
    """Walk the failover adapters in order and return their candidates.

    Caller (typically the LangGraph prices_fetch node) merges these with the
    Yahoo candidates and picks the best via select_best_price_candidate.
    """
    chain = list(adapters) if adapters else [StooqPriceAdapter(), PolygonPriceAdapter()]
    out: list[PriceSourceCandidate] = []
    for adapter in chain:
        candidate = adapter.fetch(
            isin,
            mic,
            listing_symbol=listing_symbol,
            period=period,
            currency=currency,
        )
        out.append(candidate)
        if candidate.status == "complete" and candidate.row_count > 0:
            break
    return out
