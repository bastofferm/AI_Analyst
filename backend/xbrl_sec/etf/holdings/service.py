"""Orchestration for official ETF holdings fetches."""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from xbrl_sec.etf.providers import setup_provider_registry
from xbrl_sec.sec.db.connection import connect

from .adapters import default_adapters
from .base import EtfCandidate, HoldingsAdapterError
from .writer import record_fetch_state, write_official_holdings


def _adapter_for(provider_id: str, adapters=None):
    for adapter in adapters or default_adapters():
        if adapter.supports(provider_id):
            return adapter
    return None


def select_candidates(
    *,
    provider: str = "all",
    isin: str | None = None,
    limit: int | None = None,
    refresh_all: bool = False,
    random_sample: bool = False,
) -> list[EtfCandidate]:
    where = ["COALESCE(d.is_active, TRUE)", "d.provider_id IS NOT NULL"]
    params: list[Any] = []
    if provider and provider != "all":
        params.append(provider)
        where.append("d.provider_id = %s")
    if isin:
        params.append(isin.strip().upper())
        where.append("d.isin = %s")
    if not refresh_all:
        where.append("(fs.status IS NULL OR fs.status <> 'success')")
    order_by = "random()" if random_sample and not isin else "d.aum_eur DESC NULLS LAST, d.isin"
    sql = f"""
        SELECT d.isin, d.provider_id, pv.label, d.full_name, d.short_name
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_provider pv ON pv.provider_id = d.provider_id
        LEFT JOIN sec.etf_holdings_fetch_state fs ON fs.isin = d.isin
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        EtfCandidate(
            isin=row[0],
            provider_id=row[1],
            provider_label=row[2],
            full_name=row[3],
            short_name=row[4],
        )
        for row in rows
    ]


def holdings_dry_run_summary(candidates: list[EtfCandidate]) -> dict[str, Any]:
    by_provider = Counter(candidate.provider_id for candidate in candidates)
    adapter_ids = {pid for pid in by_provider if _adapter_for(pid) is not None}
    unsupported = {pid: count for pid, count in by_provider.items() if pid not in adapter_ids}
    return {
        "candidates": len(candidates),
        "providers": dict(sorted(by_provider.items())),
        "adapter_supported_providers": sorted(adapter_ids),
        "unsupported_providers": dict(sorted(unsupported.items())),
    }


def run_holdings_fetch(
    *,
    provider: str = "all",
    isin: str | None = None,
    limit: int | None = None,
    refresh_all: bool = False,
    random_sample: bool = False,
    dry_run: bool = False,
    adapters=None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    setup_provider_registry()
    candidates = select_candidates(
        provider=provider,
        isin=isin,
        limit=limit,
        refresh_all=refresh_all,
        random_sample=random_sample,
    )
    if dry_run:
        return {"dry_run": True, **holdings_dry_run_summary(candidates)}

    stats: dict[str, Any] = {
        "dry_run": False,
        "candidates": len(candidates),
        "success": 0,
        "empty": 0,
        "unsupported": 0,
        "failed": 0,
        "rows": 0,
    }
    adapter_list = adapters or default_adapters()
    for candidate in candidates:
        adapter = _adapter_for(candidate.provider_id, adapter_list)
        if adapter is None:
            with connect() as conn:
                record_fetch_state(
                    conn,
                    isin=candidate.isin,
                    provider_id=candidate.provider_id,
                    source=None,
                    status="unsupported",
                    error_message=f"no official holdings adapter for provider {candidate.provider_id}",
                )
            stats["unsupported"] += 1
            continue
        try:
            product = adapter.resolve_product(candidate)
            result = adapter.fetch_holdings(product)
            with connect() as conn:
                written = write_official_holdings(
                    conn,
                    isin=candidate.isin,
                    provider_id=candidate.provider_id,
                    holdings=result.holdings,
                    as_of_date=result.as_of_date or as_of_date,
                    source_url=result.source_url,
                )
            if written:
                stats["success"] += 1
                stats["rows"] += written
            else:
                stats["empty"] += 1
        except HoldingsAdapterError as exc:
            with connect() as conn:
                record_fetch_state(
                    conn,
                    isin=candidate.isin,
                    provider_id=candidate.provider_id,
                    source=candidate.provider_id,
                    status="failed",
                    error_message=str(exc)[:300],
                )
            stats["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            with connect() as conn:
                record_fetch_state(
                    conn,
                    isin=candidate.isin,
                    provider_id=candidate.provider_id,
                    source=candidate.provider_id,
                    status="failed",
                    error_message=str(exc)[:300],
                )
            stats["failed"] += 1
    return stats
