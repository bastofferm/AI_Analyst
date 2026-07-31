"""ETF price-source candidates and fallback selection.

This module keeps acquisition provenance separate from the final price table:
candidate attempts can represent market prices, NAV-like prices, or non-price
performance series, but only explicitly allowed history kinds are eligible for
promotion into ``sec.fact_prices_etf``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

from .models import PriceRecord


FACT_PRICE_HISTORY_KINDS = {"market_price", "nav_price"}

_MIC_PRIORITY = {
    "XETR": 100,
    "GETT": 92,
    "XFRA": 88,
    "XSTU": 80,
    "XMUN": 72,
    "XDUS": 70,
    "XHAM": 68,
    "XHAN": 66,
    "XBER": 64,
    "TGAT": 62,
    "XWBO": 60,
}

_SOURCE_PRIORITY = {
    "yfinance.promoted": 100,
    "yfinance.exchange_ticker": 92,
    "justetf.licensed_csv": 86,
    "provider.nav": 70,
    "yfinance.isin": 40,
    "yfinance.isin_suffix": 35,
}


@dataclass(frozen=True)
class PriceSourceCandidate:
    isin: str
    source: str
    symbol: str
    mic: str
    history_kind: str = "market_price"
    status: str = "empty"
    records: tuple[PriceRecord, ...] = field(default_factory=tuple)
    first_price_date: date | None = None
    last_price_date: date | None = None
    currency: str | None = None
    error: str | None = None
    source_url: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.records)

    @property
    def history_days(self) -> int:
        if self.first_price_date is None or self.last_price_date is None:
            return 0
        return max((self.last_price_date - self.first_price_date).days, 0)

    @property
    def valid_for_fact_prices(self) -> bool:
        return (
            self.status == "complete"
            and self.row_count > 0
            and self.history_kind in FACT_PRICE_HISTORY_KINDS
        )


def select_best_price_candidate(
    candidates: Iterable[PriceSourceCandidate],
    *,
    preferred_currencies: Iterable[str | None] = (),
    allowed_history_kinds: set[str] | None = None,
) -> PriceSourceCandidate | None:
    """Pick the longest valid price history; use source/listing quality as ties."""
    allowed = allowed_history_kinds or FACT_PRICE_HISTORY_KINDS
    preferred_ccys = {str(ccy).upper() for ccy in preferred_currencies if ccy}
    valid = [
        c
        for c in candidates
        if c.status == "complete" and c.row_count > 0 and c.history_kind in allowed
    ]
    if not valid:
        return None

    def sort_key(candidate: PriceSourceCandidate) -> tuple[Any, ...]:
        ccy = str(candidate.currency or "").upper()
        return (
            candidate.history_days,
            candidate.row_count,
            candidate.last_price_date or date.min,
            _MIC_PRIORITY.get(candidate.mic, 0),
            1 if ccy in preferred_ccys else 0,
            _SOURCE_PRIORITY.get(candidate.source, 0),
            candidate.symbol,
        )

    return max(valid, key=sort_key)


def candidate_evidence(candidate: PriceSourceCandidate) -> dict[str, Any]:
    return {
        "isin": candidate.isin,
        "source": candidate.source,
        "symbol": candidate.symbol,
        "mic": candidate.mic,
        "history_kind": candidate.history_kind,
        "status": candidate.status,
        "first_price_date": candidate.first_price_date.isoformat() if candidate.first_price_date else None,
        "last_price_date": candidate.last_price_date.isoformat() if candidate.last_price_date else None,
        "row_count": candidate.row_count,
        "currency": candidate.currency,
        "error": candidate.error,
        "source_url": candidate.source_url,
        "evidence": candidate.evidence,
    }


def upsert_price_source_candidates(candidates: Iterable[PriceSourceCandidate]) -> int:
    rows = []
    now = datetime.now(timezone.utc)
    for c in candidates:
        rows.append(
            (
                c.isin,
                c.source,
                c.symbol,
                c.mic,
                c.history_kind,
                c.status,
                c.first_price_date,
                c.last_price_date,
                c.row_count,
                c.currency,
                c.error,
                c.source_url,
                json.dumps(candidate_evidence(c), ensure_ascii=False, sort_keys=True),
                now,
            )
        )
    if not rows:
        return 0
    sql = """
        INSERT INTO sec.etf_price_source_candidate
            (isin, source, symbol, mic, history_kind, status, first_price_date,
             last_price_date, price_rows, currency, error_message, source_url,
             evidence, evaluated_at)
        VALUES %s
        ON CONFLICT (isin, source, symbol, history_kind) DO UPDATE SET
            mic = EXCLUDED.mic,
            status = EXCLUDED.status,
            first_price_date = EXCLUDED.first_price_date,
            last_price_date = EXCLUDED.last_price_date,
            price_rows = EXCLUDED.price_rows,
            currency = EXCLUDED.currency,
            error_message = EXCLUDED.error_message,
            source_url = EXCLUDED.source_url,
            evidence = EXCLUDED.evidence,
            evaluated_at = EXCLUDED.evaluated_at,
            updated_at = NOW()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows, page_size=500)


class LicensedJustEtfPriceAdapter:
    """Read licensed justETF/vendor history exports from local CSV files.

    This deliberately does not scrape justETF. It is enabled only when a
    licensed export directory is explicitly configured.
    """

    source = "justetf.licensed_csv"

    def __init__(self, csv_dir: str | Path | None = None) -> None:
        self.csv_dir = Path(csv_dir).expanduser() if csv_dir else None

    @property
    def enabled(self) -> bool:
        return bool(self.csv_dir and self.csv_dir.exists())

    def fetch(self, isin: str, mic: str = "XETR") -> PriceSourceCandidate:
        isin = isin.strip().upper()
        if not self.enabled or self.csv_dir is None:
            return PriceSourceCandidate(
                isin=isin,
                source=self.source,
                symbol=isin,
                mic=mic,
                status="skipped",
                error="licensed justETF CSV directory is not configured",
            )
        path = self.csv_dir / f"{isin}.csv"
        if not path.exists():
            return PriceSourceCandidate(
                isin=isin,
                source=self.source,
                symbol=isin,
                mic=mic,
                status="empty",
                error=f"licensed CSV not found: {path}",
            )
        records: list[PriceRecord] = []
        currency = None
        history_kind = "market_price"
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    day_raw = row.get("date") or row.get("Date") or row.get("price_date")
                    close_raw = row.get("close") or row.get("Close") or row.get("nav") or row.get("NAV")
                    if not day_raw or not close_raw:
                        continue
                    day = date.fromisoformat(str(day_raw)[:10])
                    close = float(str(close_raw).replace(",", "."))
                    currency = (row.get("currency") or row.get("Currency") or currency or "").upper() or None
                    history_kind = (row.get("history_kind") or row.get("HistoryKind") or history_kind).strip() or history_kind
                    records.append(
                        PriceRecord(
                            isin=isin,
                            mic=mic,
                            price_date=day,
                            close=close,
                            open=_optional_float(row.get("open") or row.get("Open")),
                            high=_optional_float(row.get("high") or row.get("High")),
                            low=_optional_float(row.get("low") or row.get("Low")),
                            volume=_optional_int(row.get("volume") or row.get("Volume")),
                            currency=currency,
                            source=self.source,
                            history_kind=history_kind,
                            source_symbol=isin,
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - represented as candidate evidence
            return PriceSourceCandidate(
                isin=isin,
                source=self.source,
                symbol=isin,
                mic=mic,
                status="failed",
                error=str(exc)[:300],
            )
        if not records:
            return PriceSourceCandidate(
                isin=isin,
                source=self.source,
                symbol=isin,
                mic=mic,
                history_kind=history_kind,
                status="empty",
                error="licensed CSV had no usable price rows",
            )
        records.sort(key=lambda r: r.price_date)
        return PriceSourceCandidate(
            isin=isin,
            source=self.source,
            symbol=isin,
            mic=mic,
            history_kind=history_kind,
            status="complete",
            records=tuple(records),
            first_price_date=records[0].price_date,
            last_price_date=records[-1].price_date,
            currency=currency,
            source_url=str(path),
        )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None
