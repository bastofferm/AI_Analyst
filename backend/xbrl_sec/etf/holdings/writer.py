"""Database writes for official ETF holdings."""
from __future__ import annotations

from datetime import date
from typing import Iterable

from xbrl_sec.sec.db.bulk import execute_values

from .base import HoldingRow


def record_fetch_state(
    conn,
    *,
    isin: str,
    provider_id: str | None,
    status: str,
    source: str | None = None,
    row_count: int | None = None,
    source_url: str | None = None,
    as_of_date: date | None = None,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sec.etf_holdings_fetch_state
                (isin, provider_id, source, status, row_count, source_url, as_of_date,
                 last_attempt_at, last_success_at, error_message, retry_count, updated_at)
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                NOW(),
                CASE WHEN %s = 'success' THEN NOW() ELSE NULL END,
                %s,
                CASE WHEN %s = 'failed' THEN 1 ELSE 0 END,
                NOW()
            )
            ON CONFLICT (isin) DO UPDATE SET
                provider_id = EXCLUDED.provider_id,
                source = EXCLUDED.source,
                status = EXCLUDED.status,
                row_count = EXCLUDED.row_count,
                source_url = EXCLUDED.source_url,
                as_of_date = EXCLUDED.as_of_date,
                last_attempt_at = NOW(),
                last_success_at = CASE
                    WHEN EXCLUDED.status = 'success' THEN NOW()
                    ELSE sec.etf_holdings_fetch_state.last_success_at
                END,
                error_message = EXCLUDED.error_message,
                retry_count = sec.etf_holdings_fetch_state.retry_count
                    + CASE WHEN EXCLUDED.status = 'failed' THEN 1 ELSE 0 END,
                updated_at = NOW()
            """,
            (
                isin, provider_id, source, status, row_count, source_url, as_of_date,
                status, error_message, status,
            ),
        )


def write_official_holdings(
    conn,
    *,
    isin: str,
    provider_id: str,
    holdings: Iterable[HoldingRow],
    as_of_date: date | None = None,
    source_url: str | None = None,
) -> int:
    snapshot_date = as_of_date or date.today()
    profile_rows = [row.as_profile_row() for row in holdings]
    if not profile_rows:
        record_fetch_state(
            conn,
            isin=isin,
            provider_id=provider_id,
            source=provider_id,
            status="empty",
            row_count=0,
            source_url=source_url,
            as_of_date=snapshot_date,
        )
        return 0

    # Reuse the existing company/logo resolution logic from the yfinance path.
    from xbrl_sec.etf.profile import _resolve_holding_metadata

    # Metadata resolution is useful but not required for official provider rows.
    # Run it outside the write transaction so DDL/lock issues on company masters
    # cannot abort the holdings insert.
    try:
        from xbrl_sec.sec.db.connection import connect

        metadata_conn = connect()
        try:
            with metadata_conn.cursor() as cur:
                cur.execute("SET lock_timeout = '1000ms'")
            _resolve_holding_metadata(metadata_conn, profile_rows)
        finally:
            metadata_conn.rollback()
            metadata_conn.close()
    except Exception:  # noqa: BLE001 - metadata enrichment is optional for official holdings ingestion
        pass

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sec.dim_etf_profile (isin, holdings_count, profile_status)
            VALUES (%s, %s, 'complete')
            ON CONFLICT (isin) DO UPDATE SET
                holdings_count = EXCLUDED.holdings_count,
                profile_status = CASE
                    WHEN sec.dim_etf_profile.profile_status IN ('pending', 'empty', 'failed')
                    THEN 'complete'
                    ELSE sec.dim_etf_profile.profile_status
                END,
                updated_at = NOW()
            """,
            (isin, len(profile_rows)),
        )
        cur.execute("DELETE FROM sec.etf_holding WHERE isin=%s", (isin,))
        execute_values(
            cur,
            """
            INSERT INTO sec.etf_holding
                (isin, rank, symbol, holding_isin, name, weight, cik, edinet_code,
                 logo_url, resolved_company_id, resolution_source)
            VALUES %s
            """,
            [
                (
                    isin, h["rank"], h["symbol"], h["holding_isin"], h["name"], h["weight"],
                    h["cik"], h["edinet_code"], h["logo_url"], h["resolved_company_id"], h["resolution_source"],
                )
                for h in profile_rows
            ],
        )
        execute_values(
            cur,
            """
            INSERT INTO sec.etf_holding_snapshot
                (isin, as_of_date, source, rank, symbol, holding_isin, name, weight,
                 cik, edinet_code, logo_url, resolved_company_id, resolution_source)
            VALUES %s
            ON CONFLICT (isin, as_of_date, source, rank) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                holding_isin = EXCLUDED.holding_isin,
                name = EXCLUDED.name,
                weight = EXCLUDED.weight,
                cik = EXCLUDED.cik,
                edinet_code = EXCLUDED.edinet_code,
                logo_url = EXCLUDED.logo_url,
                resolved_company_id = EXCLUDED.resolved_company_id,
                resolution_source = EXCLUDED.resolution_source,
                fetched_at = NOW()
            """,
            [
                (
                    isin, snapshot_date, provider_id, h["rank"], h["symbol"], h["holding_isin"],
                    h["name"], h["weight"], h["cik"], h["edinet_code"], h["logo_url"],
                    h["resolved_company_id"], h["resolution_source"],
                )
                for h in profile_rows
            ],
        )

    record_fetch_state(
        conn,
        isin=isin,
        provider_id=provider_id,
        source=provider_id,
        status="success",
        row_count=len(profile_rows),
        source_url=source_url,
        as_of_date=snapshot_date,
    )
    return len(profile_rows)
