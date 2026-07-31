"""JP EDINET pipeline adapter for the new single fact table design."""
from __future__ import annotations

from datetime import date
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from xbrl_sec.sec.parsers.edinet_xbrl import extract_identity_metadata, parse_xbrl_file
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.edinet_download import (
    download_xbrl_packages,
    index_filings,
    refresh_master,
)
from xbrl_sec.sec.sources.edinet_forms import normalize_doc_type_codes
from xbrl_sec.sec.sources.edinet_filings import (
    changed_or_unparsed_xbrl,
    count_xbrl_files_after,
    extract_xbrl_packages,
    sync_xbrl_index,
)
from xbrl_sec.sec.sources.company_enrichment import enrich_gics, enrich_isin, enrich_jp_identity_from_xbrl_metadata
from xbrl_sec.sec.sources.master_sync import sync_master_dimensions
from xbrl_sec.sec.quality.validate import assert_jp_master_quality
from xbrl_sec.sec.state.store import (
    finish_run,
    mark_source_filings_parsed,
    record_stage_event,
    record_entity_state,
    reset_downstream,
    start_run,
    update_run_progress,
    update_source_filing_payload,
)
from xbrl_sec.sec.std.jp_standardize import populate_jp_std
from xbrl_sec.sec.metrics.compute import compute_metrics
from xbrl_sec.sec.metrics.recon import build_recon
from xbrl_sec.sec.writers.raw_facts import upsert_jp_facts


STAGES = (
    "company_master",
    "filing_index",
    "zip_download",
    "xbrl_package_download",
    "companyfacts_extract",
    "raw_parse",
    "gics",
    "isin",
    "standardize",
    "ticker_map",
    "metrics",
    "recon",
    "validate",
)


def _log_jp(stage: str, message: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = f" {details}" if details else ""
    print(f"{datetime.now().isoformat(timespec='seconds')} JP {stage}: {message}{suffix}", flush=True)


def _record_source_progress(ctx, payload: dict[str, Any]) -> None:
    event_type = str(payload.get("event_type") or "stage_progress")
    message = str(payload.get("message") or f"{ctx.stage} progress")
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    entity_id = current.get("entity_id") if current else None
    rows_in = int(payload.get("rows_in") or payload.get("total") or payload.get("processed") or 0)
    rows_out = int(payload.get("rows_out") or payload.get("downloaded") or payload.get("ok") or 0)
    update_run_progress(ctx, rows_in=rows_in, rows_out=rows_out)
    record_stage_event(ctx, event_type, message, payload, entity_id=str(entity_id) if entity_id else None)


def source_dir() -> Path:
    return load_settings().market_data_root / "japan_edinet"


def _active_scope(entity_ids: list[str] | None = None) -> list[str]:
    if entity_ids is not None:
        return [str(value).strip() for value in entity_ids if str(value).strip()]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT edinet_code
            FROM dim_company_jp
            WHERE edinet_code IS NOT NULL
              AND include_in_pipeline
            ORDER BY edinet_code
            """
        )
        return [row[0] for row in cur.fetchall()]


def _chunks(values: list[str], size: int):
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _local_rebuild_stage(filed_date_max: date | None) -> str:
    suffix = filed_date_max.isoformat() if filed_date_max else "all"
    return f"local_rebuild:{suffix}"


def _completed_rebuild_entities(stage: str) -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id
            FROM pipeline_entity_state
            WHERE jurisdiction='JP'
              AND stage=%s
              AND status='succeeded'
            """,
            (stage,),
        )
        return {row[0] for row in cur.fetchall()}


def _raw_counts_by_entity(entity_ids: list[str]) -> dict[str, int]:
    if not entity_ids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT edinet_code, COUNT(*)
            FROM fact_fundamentals_jp
            WHERE edinet_code = ANY(%s)
            GROUP BY edinet_code
            """,
            (entity_ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _max_filed_by_entity(entity_ids: list[str]) -> dict[str, date | None]:
    if not entity_ids:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT edinet_code, MAX(filed_date)
            FROM fact_fundamentals_jp
            WHERE edinet_code = ANY(%s)
            GROUP BY edinet_code
            """,
            (entity_ids,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def replenish_jp_pipeline_scope(group: str = "pilot_500_jp", target: int = 500) -> dict[str, object]:
    """Activate valid inactive JP master rows until a sample group reaches target size."""
    if target <= 0:
        raise ValueError("target must be positive")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_jp
            WHERE include_in_pipeline
              AND pipeline_sample_group = %s
            """,
            (group,),
        )
        active_before = cur.fetchone()[0]
        needed = max(0, target - active_before)
        activated: list[tuple[str, str | None, str | None]] = []
        if needed:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT edinet_code
                    FROM dim_company_jp
                    WHERE NOT include_in_pipeline
                      AND primary_ticker IS NOT NULL
                      AND COALESCE(is_active, true)
                      AND mapping_sector IS NOT NULL
                    ORDER BY
                        CASE WHEN isin IS NOT NULL THEN 0 ELSE 1 END,
                        CASE WHEN gics_sector_code IS NOT NULL THEN 0 ELSE 1 END,
                        edinet_code
                    LIMIT %s
                )
                UPDATE dim_company_jp d
                   SET include_in_pipeline = true,
                       pipeline_sample_group = %s,
                       updated_at = now()
                  FROM candidates c
                 WHERE d.edinet_code = c.edinet_code
                RETURNING d.edinet_code, d.primary_ticker, d.name
                """,
                (needed, group),
            )
            activated = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_jp
            WHERE include_in_pipeline
              AND pipeline_sample_group = %s
            """,
            (group,),
        )
        active_after = cur.fetchone()[0]
    assert_jp_master_quality()
    return {
        "group": group,
        "target": target,
        "active_before": active_before,
        "active_after": active_after,
        "activated": [
            {"edinet_code": row[0], "ticker": row[1], "name": row[2]}
            for row in activated
        ],
    }


def activate_all_eligible_jp_pipeline_scope(group: str = "jp_active_full_20260524") -> dict[str, object]:
    """Activate all active listed JP companies and remove stale ineligible actives."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM dim_company_jp WHERE include_in_pipeline")
        active_before = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE dim_company_jp
               SET include_in_pipeline = false,
                   pipeline_sample_group = NULL,
                   updated_at = now()
             WHERE include_in_pipeline
               AND NOT (
                    primary_ticker IS NOT NULL
                AND COALESCE(is_active, true)
                AND mapping_sector IS NOT NULL
               )
            RETURNING edinet_code, primary_ticker, name
            """
        )
        deactivated = cur.fetchall()
        cur.execute(
            """
            WITH candidates AS (
                SELECT edinet_code
                FROM dim_company_jp
                WHERE NOT include_in_pipeline
                  AND primary_ticker IS NOT NULL
                  AND COALESCE(is_active, true)
                  AND mapping_sector IS NOT NULL
            )
            UPDATE dim_company_jp d
               SET include_in_pipeline = true,
                   pipeline_sample_group = COALESCE(d.pipeline_sample_group, %s),
                   updated_at = now()
              FROM candidates c
             WHERE d.edinet_code = c.edinet_code
            RETURNING d.edinet_code, d.primary_ticker, d.name
            """,
            (group,),
        )
        activated = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM dim_company_jp WHERE include_in_pipeline")
        active_after = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_jp
            WHERE primary_ticker IS NOT NULL
              AND COALESCE(is_active, true)
              AND mapping_sector IS NOT NULL
            """
        )
        eligible = cur.fetchone()[0]
    assert_jp_master_quality()
    return {
        "group": group,
        "eligible": eligible,
        "active_before": active_before,
        "active_after": active_after,
        "activated_count": len(activated),
        "deactivated_count": len(deactivated),
        "activated": [
            {"edinet_code": row[0], "ticker": row[1], "name": row[2]}
            for row in activated[:50]
        ],
        "deactivated": [
            {"edinet_code": row[0], "ticker": row[1], "name": row[2]}
            for row in deactivated[:50]
        ],
    }


def refresh_jp_master(
    full: bool = False,
    days: int = 400,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    ctx = start_run("JP", "company_master", "full_refresh" if full else "incremental")
    try:
        _log_jp("company_master", "start", full=full, days=days, start_date=start_date, end_date=end_date)
        rows = refresh_master(full=full, days=days, start_date=start_date, end_date=end_date)
        master_counts = sync_master_dimensions("JP")
        assert_jp_master_quality()
        finish_run(ctx, "succeeded", rows_out=rows + master_counts["ticker_links"])
        _log_jp("company_master", "finished", rows=rows, ticker_links=master_counts["ticker_links"])
        return rows
    except Exception as exc:
        _log_jp("company_master", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def index_jp_api(
    full: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "filing_index_api", "full_refresh" if full else "incremental")
    try:
        _log_jp("filing_index_api", "start", full=full, start_date=start_date, end_date=end_date, filing_types=",".join(doc_types) if doc_types else None)
        rows = index_filings(full=full, start_date=start_date, end_date=end_date, filing_types=doc_types)
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_jp("filing_index_api", "finished", rows=rows)
        return rows
    except Exception as exc:
        _log_jp("filing_index_api", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def enrich_jp_gics(full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    ctx = start_run("JP", "gics", "full_refresh" if full else "incremental")
    try:
        rows = enrich_gics("JP", full=full, max_tickers=max_tickers)
        finish_run(ctx, "succeeded", rows_in=rows.get("candidates"), rows_out=rows.get("updated"))
        return rows
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def enrich_jp_isin(full: bool = False, max_tickers: int | None = None) -> dict[str, int]:
    ctx = start_run("JP", "isin", "full_refresh" if full else "incremental")
    try:
        rows = enrich_isin("JP", full=full, max_tickers=max_tickers)
        finish_run(ctx, "succeeded", rows_in=rows.get("candidates"), rows_out=rows.get("found", 0) + rows.get("name_found", 0))
        return rows
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def enrich_jp_identity(full: bool = False) -> dict[str, int]:
    ctx = start_run("JP", "identity", "full_refresh" if full else "incremental")
    try:
        updated = enrich_jp_identity_from_xbrl_metadata(full=full)
        finish_run(ctx, "succeeded", rows_out=updated)
        return {"updated": updated}
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def refresh_jp_master_only(
    full: bool = False,
    days: int = 400,
    start_date: date | None = None,
    end_date: date | None = None,
    max_tickers: int | None = None,
) -> dict[str, object]:
    ctx = start_run("JP", "master_only", "full_refresh" if full else "incremental")
    try:
        master_rows = refresh_jp_master(
            full=full,
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
        identity = enrich_jp_identity(full=full)
        gics = enrich_jp_gics(full=full, max_tickers=max_tickers)
        isin = enrich_jp_isin(full=full, max_tickers=max_tickers)
        master_counts = sync_master_dimensions("JP")
        assert_jp_master_quality()
        rows_out = (
            master_rows
            + identity.get("updated", 0)
            + gics.get("updated", 0)
            + isin.get("found", 0)
            + isin.get("name_found", 0)
        )
        finish_run(ctx, "succeeded", rows_out=rows_out + master_counts["ticker_links"])
        return {
            "master_rows": master_rows,
            "identity": identity,
            "gics": gics,
            "isin": isin,
            "sync": master_counts,
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def download_jp_xbrl(
    force: bool = False,
    limit: int | None = None,
    doc_ids: list[str] | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> tuple[int, int]:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "xbrl_package_download", "full_refresh" if force else "incremental")
    try:
        _log_jp("xbrl_package_download", "start", force=force, limit=limit, doc_ids=len(doc_ids or []), filing_types=",".join(doc_types) if doc_types else None)
        stats = download_xbrl_packages(
            force=force,
            limit=limit,
            doc_ids=doc_ids,
            filing_types=doc_types,
            progress_callback=lambda payload: _record_source_progress(ctx, payload),
        )
        # ok = documents now satisfied (freshly fetched or already on disk);
        # err = real failures only (EDINET not-found is tracked separately).
        ok = stats["fetched"] + stats["skipped_existing"]
        err = stats["errors"]
        candidates = ok + stats["not_found"] + err
        finish_run(ctx, "succeeded", rows_in=candidates, rows_out=ok,
                   error=None if err == 0 else f"{err} errors")
        _log_jp("xbrl_package_download", "finished",
                fetched=stats["fetched"], skipped_existing=stats["skipped_existing"],
                not_found=stats["not_found"], unavailable=stats["unavailable"], errors=err)
        return ok, err
    except Exception as exc:
        _log_jp("xbrl_package_download", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def index_jp_xbrl(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    force_resync: bool = False,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "filing_index", "incremental")
    try:
        scope_log = entity_ids if doc_ids else _active_scope(entity_ids)
        _log_jp(
            "filing_index",
            "start",
            scope=len(scope_log or []),
            doc_ids=len(doc_ids or []),
            filed_date_max=filed_date_max,
            filing_types=",".join(doc_types) if doc_types else None,
            force_resync=force_resync,
        )

        def _progress(total: dict[str, int | str]) -> None:
            files = int(total.get("files") or 0)
            rows_seen = int(total.get("rows") or 0)
            update_run_progress(ctx, rows_in=files, rows_out=rows_seen)
            _log_jp("filing_index", "progress", phase=total.get("phase"), files=files, rows=rows_seen)

        # Pass entity_ids unchanged (not the pre-resolved active scope) so
        # unscoped pipeline runs let sync_xbrl_index take the freshness
        # short-circuit. An explicit entity list still constrains the scan.
        rows = sync_xbrl_index(
            entity_ids,
            doc_ids,
            filed_date_max=filed_date_max,
            filing_types=doc_types,
            progress_callback=_progress,
            force_resync=force_resync,
        )
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_jp("filing_index", "finished", rows=rows)
        return rows
    except Exception as exc:
        _log_jp("filing_index", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def extract_jp_xbrl(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "xbrl_package_extract", "reparse" if force else "incremental")
    try:
        scope = entity_ids if doc_ids else _active_scope(entity_ids)
        _log_jp("xbrl_package_extract", "start", scope=len(scope or []), doc_ids=len(doc_ids or []), force=force, filing_types=",".join(doc_types) if doc_types else None)

        def _progress(processed: int) -> None:
            update_run_progress(ctx, rows_in=processed)
            _log_jp("xbrl_package_extract", "progress", processed=processed)

        rows = extract_xbrl_packages(
            entity_ids=scope,
            doc_ids=doc_ids,
            force=force,
            filing_types=doc_types,
            progress_callback=_progress,
        )
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_jp("xbrl_package_extract", "finished", rows=rows)
        return rows
    except Exception as exc:
        _log_jp("xbrl_package_extract", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def parse_jp_raw(
    entity_ids: list[str] | None = None,
    doc_ids: list[str] | None = None,
    force: bool = False,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    sync_index: bool = True,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "raw_parse", "reparse" if force else "incremental")
    total_rows = 0
    total_files = 0
    total_errors = 0
    agg_stats = {"kept": 0}
    try:
        scope = entity_ids if doc_ids else _active_scope(entity_ids)
        _log_jp(
            "raw_parse",
            "start",
            scope=len(scope or []),
            doc_ids=len(doc_ids or []),
            force=force,
            filed_date_max=filed_date_max,
            filing_types=",".join(doc_types) if doc_types else None,
            sync_index=sync_index,
        )
        _log_jp("raw_parse", "future-file count start", enabled=bool(filed_date_max))
        excluded_future = count_xbrl_files_after(filed_date_max, scope, doc_ids, filing_types=doc_types) if filed_date_max else 0
        _log_jp("raw_parse", "future-file count finished", excluded=excluded_future)
        if sync_index:
            _log_jp("raw_parse", "xbrl index sync start")

            def _index_progress(total: dict[str, int | str]) -> None:
                files_seen = int(total.get("files") or 0)
                rows_seen = int(total.get("rows") or 0)
                update_run_progress(ctx, rows_in=files_seen, rows_out=rows_seen)
                _log_jp("raw_parse", "xbrl index progress", phase=total.get("phase"), files=files_seen, rows=rows_seen)

            indexed = sync_xbrl_index(
                scope,
                doc_ids,
                filed_date_max=filed_date_max,
                filing_types=doc_types,
                progress_callback=_index_progress,
            )
            _log_jp("raw_parse", "xbrl index sync finished", rows=indexed)

        def _candidate_progress(total: dict[str, int | str]) -> None:
            files_seen = int(total.get("files") or 0)
            rows_seen = int(total.get("rows") or 0)
            update_run_progress(ctx, rows_in=files_seen, rows_out=rows_seen)
            _log_jp("raw_parse", "candidate progress", phase=total.get("phase"), files=files_seen, candidates=rows_seen)

        _log_jp("raw_parse", "candidate selection start")
        files = changed_or_unparsed_xbrl(
            scope,
            doc_ids,
            force=force,
            filed_date_max=filed_date_max,
            filing_types=doc_types,
            hash_files=not force,
            progress_callback=_candidate_progress,
        )
        _log_jp("raw_parse", "candidate selection finished", candidates=len(files))
        for item in files:
            total_files += 1
            try:
                identity = extract_identity_metadata(item)
                file_stats: dict[str, int] = {}
                parsed_rows = parse_xbrl_file(item, file_stats)
                for k, v in file_stats.items():
                    agg_stats[k] = agg_stats.get(k, 0) + v
                rows_out = upsert_jp_facts(parsed_rows) if parsed_rows else 0
                total_rows += rows_out
                update_source_filing_payload("JP", item.doc_id, {"jp_identity": identity})
                mark_source_filings_parsed("JP", {item.doc_id}, parsed=True)
                record_entity_state(
                    ctx,
                    entity_id=item.edinet_code,
                    source_hash=item.source_hash,
                    max_filed_date=item.filed_date,
                    rows_in=len(parsed_rows),
                    rows_out=rows_out,
                    status="succeeded",
                )
                if total_files == 1 or total_files % 25 == 0:
                    update_run_progress(ctx, rows_in=total_files, rows_out=total_rows)
                    _log_jp("raw_parse", "parse progress", files=total_files, rows=total_rows, errors=total_errors, last_doc=item.doc_id, last_rows=rows_out)
            except Exception as item_exc:
                total_errors += 1
                message = str(item_exc)[:2000]
                mark_source_filings_parsed("JP", {item.doc_id}, parsed=False, error=message)
                record_entity_state(
                    ctx,
                    entity_id=item.edinet_code,
                    source_hash=item.source_hash,
                    max_filed_date=item.filed_date,
                    rows_in=0,
                    rows_out=0,
                    status="failed",
                )
                if total_errors == 1 or total_errors % 10 == 0:
                    _log_jp("raw_parse", "parse item failed", files=total_files, errors=total_errors, doc=item.doc_id, error=message)
                if total_files % 25 == 0:
                    update_run_progress(ctx, rows_in=total_files, rows_out=total_rows)
        enrich_jp_identity(full=force)
        print(f"jp_raw_parse_drop_stats: {agg_stats}", flush=True)
        if filed_date_max:
            print(f"jp_raw_parse_excluded_after_{filed_date_max.isoformat()}: {excluded_future}", flush=True)
        finish_run(
            ctx,
            "succeeded",
            rows_in=total_files,
            rows_out=total_rows,
            error=None if total_errors == 0 else f"{total_errors} filing parse errors",
        )
        _log_jp("raw_parse", "finished", files=total_files, rows=total_rows, errors=total_errors)
        return total_rows
    except Exception as exc:
        _log_jp("raw_parse", "failed", files=total_files, rows=total_rows, errors=total_errors, error=str(exc))
        finish_run(ctx, "failed", rows_in=total_files, rows_out=total_rows, error=str(exc))
        raise


def rebuild_jp_local(
    entity_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    chunk_size: int = 25,
    resume: bool = True,
    downstream: bool = True,
    sync_index: bool = False,
) -> dict[str, int]:
    """Safely rebuild JP from local XBRL files in resumable entity chunks.

    This intentionally avoids global reset semantics. Each chunk is reset,
    parsed, standardized, metric-computed, and reconciled before the next chunk
    is touched, so an interruption leaves at most one chunk incomplete.
    """
    doc_types = normalize_doc_type_codes(filing_types)
    stage = _local_rebuild_stage(filed_date_max)
    scope = _active_scope(entity_ids)
    if resume:
        completed = _completed_rebuild_entities(stage)
        scope = [entity for entity in scope if entity not in completed]
    ctx = start_run(
        "JP",
        stage,
        "reparse",
        json.dumps(
            {
                "entities": len(scope),
                "filed_date_max": filed_date_max.isoformat() if filed_date_max else None,
                "filing_types": doc_types,
                "chunk_size": chunk_size,
                "resume": resume,
                "downstream": downstream,
                "sync_index": sync_index,
            },
            sort_keys=True,
        ),
    )
    processed_entities = 0
    raw_rows = 0
    std_rows = 0
    metric_rows = 0
    recon_rows = 0
    try:
        sync_master_dimensions("JP")
        assert_jp_master_quality()
        for chunk in _chunks(scope, chunk_size):
            files = changed_or_unparsed_xbrl(
                chunk,
                force=True,
                filed_date_max=filed_date_max,
                filing_types=doc_types,
                hash_files=False,
            )
            try:
                reset_downstream("JP", chunk)
                raw_written = parse_jp_raw(
                    entity_ids=chunk,
                    force=True,
                    filed_date_max=filed_date_max,
                    filing_types=doc_types,
                    sync_index=sync_index,
                )
                raw_rows += raw_written
                if downstream:
                    std_rows += populate_jp_std(entity_ids=chunk, full=False)
                    metric_rows += compute_metrics("JP", entity_ids=chunk, full=False)
                    recon_rows += build_recon("JP", entity_ids=chunk, full=False)
                counts = _raw_counts_by_entity(chunk)
                max_filed = _max_filed_by_entity(chunk)
                files_by_entity: dict[str, int] = {}
                for item in files:
                    files_by_entity[item.edinet_code] = files_by_entity.get(item.edinet_code, 0) + 1
                for entity in chunk:
                    record_entity_state(
                        ctx,
                        entity_id=entity,
                        source_hash=None,
                        max_filed_date=max_filed.get(entity),
                        rows_in=files_by_entity.get(entity, 0),
                        rows_out=counts.get(entity, 0),
                        status="succeeded",
                    )
            except Exception:
                for entity in chunk:
                    record_entity_state(
                        ctx,
                        entity_id=entity,
                        source_hash=None,
                        max_filed_date=None,
                        rows_in=0,
                        rows_out=0,
                        status="failed",
                    )
                raise
            processed_entities += len(chunk)
            update_run_progress(ctx, rows_in=processed_entities, rows_out=raw_rows)
            print(
                "jp_local_rebuild_progress: "
                f"entities={processed_entities}/{len(scope)} raw_rows={raw_rows} "
                f"std_rows={std_rows} metric_rows={metric_rows} recon_rows={recon_rows}",
                flush=True,
            )
        finish_run(ctx, "succeeded", rows_in=processed_entities, rows_out=raw_rows)
        return {
            "entities": processed_entities,
            "raw_rows": raw_rows,
            "std_rows": std_rows,
            "metric_rows": metric_rows,
            "recon_rows": recon_rows,
        }
    except Exception as exc:
        finish_run(ctx, "failed", rows_in=processed_entities, rows_out=raw_rows, error=str(exc))
        raise


def plan_jp_local_rebuild(
    entity_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    chunk_size: int = 25,
    resume: bool = True,
) -> dict[str, int]:
    doc_types = normalize_doc_type_codes(filing_types)
    stage = _local_rebuild_stage(filed_date_max)
    scope = _active_scope(entity_ids)
    completed = _completed_rebuild_entities(stage) if resume else set()
    remaining = [entity for entity in scope if entity not in completed]
    files = changed_or_unparsed_xbrl(
        remaining,
        force=True,
        filed_date_max=filed_date_max,
        filing_types=doc_types,
        hash_files=False,
    )
    future = count_xbrl_files_after(filed_date_max, remaining, filing_types=doc_types) if filed_date_max else 0
    return {
        "active_scope_entities": len(scope),
        "completed_entities": len(completed & set(scope)),
        "remaining_entities": len(remaining),
        "chunks": (len(remaining) + chunk_size - 1) // chunk_size if remaining else 0,
        "local_files": len(files),
        "local_file_entities": len({item.edinet_code for item in files}),
        "excluded_future_files": future,
    }


def reparse(
    entity_ids: list[str] | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> None:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "raw_parse", "reparse")
    try:
        _log_jp("reparse", "start", entity_ids=len(entity_ids or []), filed_date_max=filed_date_max, filing_types=",".join(doc_types) if doc_types else None)
        sync_master_dimensions("JP")
        assert_jp_master_quality()
        scope = _active_scope(entity_ids)
        reset_downstream("JP", None if entity_ids is None else scope)
        rows = parse_jp_raw(entity_ids=scope, force=True, filed_date_max=filed_date_max, filing_types=doc_types)
        _log_jp("standardize", "start", scope=len(scope), full=entity_ids is None)
        populate_jp_std(entity_ids=None if entity_ids is None else scope, full=entity_ids is None)
        _log_jp("standardize", "finished")
        _log_jp("metrics", "start", scope=len(scope), full=entity_ids is None)
        compute_metrics("JP", entity_ids=None if entity_ids is None else scope, full=entity_ids is None)
        _log_jp("metrics", "finished")
        _log_jp("recon", "start", scope=len(scope), full=entity_ids is None)
        build_recon("JP", entity_ids=None if entity_ids is None else scope, full=entity_ids is None)
        _log_jp("recon", "finished")
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_jp("reparse", "finished", rows=rows)
    except Exception as exc:
        _log_jp("reparse", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def run_incremental(
    download: bool = False,
    full: bool = False,
    limit: int | None = None,
    filed_date_max: date | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    doc_types = normalize_doc_type_codes(filing_types)
    ctx = start_run("JP", "pipeline", "incremental")
    try:
        _log_jp("pipeline", "start", download=download, full=full, limit=limit, filed_date_max=filed_date_max, filing_types=",".join(doc_types) if doc_types else None)
        if not source_dir().exists():
            raise FileNotFoundError(source_dir())
        if download:
            refresh_jp_master(full=full)
            enrich_jp_gics(full=full)
            enrich_jp_isin(full=full)
            index_jp_api(full=full, end_date=filed_date_max, filing_types=doc_types)
            download_jp_xbrl(force=False, limit=limit, filing_types=doc_types)
        sync_master_dimensions("JP")
        assert_jp_master_quality()
        scope = _active_scope()
        _log_jp("pipeline", "active scope loaded", scope=len(scope))
        extract_jp_xbrl(entity_ids=scope, force=False, filing_types=doc_types)
        # Pass entity_ids=None to index_jp_xbrl on full pipeline runs so the
        # sync_xbrl_index freshness marker can short-circuit when the
        # filesystem hasn't changed since the last successful sweep. Passing
        # the active scope explicitly would always trip user_scoped=True and
        # force a full ~4h rescan even on resumes.
        index_jp_xbrl(entity_ids=None, filed_date_max=filed_date_max, filing_types=doc_types)
        rows = parse_jp_raw(entity_ids=scope, force=False, filed_date_max=filed_date_max, filing_types=doc_types)
        _log_jp("standardize", "start", scope=len(scope), full=False)
        populate_jp_std(full=False)
        _log_jp("standardize", "finished")
        _log_jp("metrics", "start", scope=len(scope), full=False)
        compute_metrics("JP", full=False)
        _log_jp("metrics", "finished")
        _log_jp("recon", "start", scope=len(scope), full=False)
        build_recon("JP", full=False)
        _log_jp("recon", "finished")
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_jp("pipeline", "finished", rows=rows)
        return rows
    except Exception as exc:
        _log_jp("pipeline", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise
