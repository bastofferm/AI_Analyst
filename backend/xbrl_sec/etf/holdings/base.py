"""Base types for official ETF holdings adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class HoldingsAdapterError(RuntimeError):
    """Base error for provider adapter failures."""


class ProductResolutionError(HoldingsAdapterError):
    """Raised when an adapter cannot resolve an ISIN to a provider product."""


class HoldingsParseError(HoldingsAdapterError):
    """Raised when provider content cannot be parsed as holdings."""


@dataclass(frozen=True)
class EtfCandidate:
    isin: str
    provider_id: str
    provider_label: str | None = None
    full_name: str | None = None
    short_name: str | None = None


@dataclass(frozen=True)
class ProductRef:
    isin: str
    provider_id: str
    product_url: str | None = None
    download_url: str | None = None
    source_name: str | None = None


@dataclass(frozen=True)
class HoldingRow:
    rank: int
    symbol: str | None
    holding_isin: str | None
    name: str | None
    weight: float | None

    def as_profile_row(self) -> dict:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "holding_isin": self.holding_isin,
            "name": self.name,
            "weight": self.weight,
            "cik": None,
            "edinet_code": None,
            "logo_url": None,
            "resolved_company_id": None,
            "resolution_source": None,
        }


@dataclass(frozen=True)
class HoldingsResult:
    isin: str
    provider_id: str
    holdings: list[HoldingRow]
    source_url: str | None = None
    as_of_date: date | None = None


class HoldingsAdapter(Protocol):
    provider_ids: tuple[str, ...]

    def supports(self, provider_id: str) -> bool:
        ...

    def resolve_product(self, candidate: EtfCandidate) -> ProductRef:
        ...

    def fetch_holdings(self, product: ProductRef) -> HoldingsResult:
        ...
