"""Plain dataclasses for ETF pipeline records (WA0006 §4 EtfRecord/ListingRecord/PriceRecord)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# DE/AT trading-venue MICs (WA0006 §2.1). Primary ETF venue filter — the spec
# notes MIC filtering is more reliable than CFI for identifying DE/AT ETFs.
DE_AT_MICS: dict[str, str] = {
    "XETR": "DE",  # Xetra
    "XFRA": "DE",  # Frankfurt Stock Exchange
    "XDUS": "DE",  # Düsseldorf
    "XHAM": "DE",  # Hamburg
    "XMUN": "DE",  # Munich
    "XSTU": "DE",  # Stuttgart
    "XWBO": "AT",  # Wiener Börse
}


@dataclass
class EtfRecord:
    isin: str
    full_name: str
    short_name: str | None = None
    issuer_lei: str | None = None
    fund_currency: str | None = None
    cfi: str | None = None
    termination_date: date | None = None


@dataclass
class ListingRecord:
    isin: str
    mic: str
    trading_currency: str | None = None
    country: str | None = None


@dataclass
class PriceRecord:
    isin: str
    mic: str
    price_date: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    currency: str | None = None
    source: str = "yfinance"
    history_kind: str = "market_price"
    source_symbol: str | None = None
