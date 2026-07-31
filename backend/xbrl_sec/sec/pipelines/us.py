"""US SEC pipeline adapter for the new single fact table design."""
from __future__ import annotations

from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

from xbrl_sec.sec.parsers.sec_companyfacts import parse_companyfacts_file
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.sources.sec_download import download_companyfacts_for_master, refresh_master, sync_submissions_filing_index
from xbrl_sec.sec.sources.sec_filings import changed_or_unparsed_files, normalize_cik, sync_companyfacts_index
from xbrl_sec.sec.sources.sec_forms import normalize_form
from xbrl_sec.sec.sources.master_sync import sync_master_dimensions
from xbrl_sec.sec.sources.sec_xbrl import download_us_xbrl_zips, extract_us_linkbases, reconcile_local_xbrl_inventory
from xbrl_sec.sec.quality.validate import assert_us_master_quality
from xbrl_sec.sec.state.store import (
    finish_run,
    mark_source_entity_filings_parsed,
    mark_source_filings_parsed,
    record_stage_event,
    record_entity_state,
    reset_downstream,
    start_run,
    update_run_progress,
)
from xbrl_sec.sec.std.us_standardize import populate_us_std
from xbrl_sec.sec.metrics.compute import compute_metrics
from xbrl_sec.sec.metrics.recon import build_recon
from xbrl_sec.sec.writers.raw_facts import upsert_us_facts


STAGES = (
    "master",
    "companyfacts_download",
    "xbrl_package_download",
    "filing_index",
    "linkbase_extract",
    "raw_parse",
    "standardize",
    "ticker_map",
    "metrics",
    "recon",
    "validate",
)
US_10K_10Q_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")
_US_10K_10Q_FORM_SET = frozenset(US_10K_10Q_FORMS)


def normalize_us_filing_types(filing_types: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...] | None:
    if filing_types is None:
        return None
    forms: list[str] = []
    for value in filing_types:
        form = normalize_form(value)
        if not form:
            continue
        if form not in _US_10K_10Q_FORM_SET:
            raise ValueError(f"Unsupported US filing type {value!r}; choose from {', '.join(US_10K_10Q_FORMS)}")
        if form not in forms:
            forms.append(form)
    if not forms:
        raise ValueError("At least one US filing type must be selected")
    return tuple(forms)


def _filing_type_filter(filing_types: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...] | None:
    return normalize_us_filing_types(filing_types)


def _current_period_forms(filing_types: tuple[str, ...] | None = None) -> tuple[str, ...]:
    return filing_types or US_10K_10Q_FORMS


def _log_us(stage: str, message: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    suffix = f" {details}" if details else ""
    print(f"{datetime.now().isoformat(timespec='seconds')} US {stage}: {message}{suffix}", flush=True)


def _record_source_progress(ctx, payload: dict[str, Any]) -> None:
    event_type = str(payload.get("event_type") or "stage_progress")
    message = str(payload.get("message") or f"{ctx.stage} progress")
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    entity_id = (current.get("entity_id") or current.get("cik")) if current else None
    rows_in = int(payload.get("rows_in") or payload.get("total") or payload.get("processed") or 0)
    rows_out = int(payload.get("rows_out") or payload.get("downloaded") or payload.get("ok") or 0)
    update_run_progress(ctx, rows_in=rows_in, rows_out=rows_out)
    record_stage_event(ctx, event_type, message, payload, entity_id=str(entity_id) if entity_id else None)


def source_dir() -> Path:
    return load_settings().market_data_root / "us_sec"


def _master_scope(max_ciks: int | None = None, include_in_pipeline: bool = True) -> list[str]:
    with connect() as conn, conn.cursor() as cur:
        sql = """
            SELECT cik
            FROM dim_company_us
            WHERE cik IS NOT NULL
              AND include_in_pipeline = %s
            ORDER BY cik
        """
        params = (include_in_pipeline,)
        if max_ciks:
            sql += " LIMIT %s"
            params = (include_in_pipeline, max_ciks)
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def _entity_scope(
    entity_ids: list[str] | None = None,
    max_ciks: int | None = None,
    include_in_pipeline: bool = True,
) -> list[str]:
    if entity_ids is not None:
        return [normalize_cik(v) for v in entity_ids]
    return _master_scope(max_ciks, include_in_pipeline=include_in_pipeline) or []


def prune_us_state_to_active_scope() -> dict[str, int]:
    """Keep default US source/pipeline state scoped to active dim_company_us rows."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM source_filing_state s
             WHERE s.jurisdiction = 'US'
               AND NOT EXISTS (
                    SELECT 1 FROM dim_company_us c
                     WHERE c.cik = s.entity_id
                       AND c.include_in_pipeline
               )
            """
        )
        source_rows = cur.rowcount
        cur.execute(
            """
            DELETE FROM pipeline_entity_state p
             WHERE p.jurisdiction = 'US'
               AND NOT EXISTS (
                    SELECT 1 FROM dim_company_us c
                     WHERE c.cik = p.entity_id
                       AND c.include_in_pipeline
               )
            """
        )
        entity_rows = cur.rowcount
    return {"source_filing_state": source_rows, "pipeline_entity_state": entity_rows}


def refresh_us_master(download: bool = False, full: bool = False, max_ciks: int | None = None) -> int:
    ctx = start_run("US", "master", "full_refresh" if full else "incremental")
    try:
        _log_us("master", "start", download=download, full=full, max_ciks=max_ciks)
        rows = refresh_master(full=full, max_ciks=max_ciks, download=download)
        master_counts = sync_master_dimensions("US")
        assert_us_master_quality()
        finish_run(ctx, "succeeded", rows_out=rows + master_counts["ticker_links"])
        _log_us("master", "finished", rows=rows, ticker_links=master_counts["ticker_links"])
        return rows
    except Exception as exc:
        _log_us("master", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def replenish_us_pipeline_scope(group: str = "pilot_50_us", target: int = 50) -> dict[str, object]:
    """Activate valid inactive US master rows until a sample group reaches target size."""
    if target <= 0:
        raise ValueError("target must be positive")
    assert_us_master_quality()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH stale_parsed AS (
                SELECT d.cik,
                       row_number() OVER (ORDER BY d.cik) AS rn
                FROM dim_company_us d
                WHERE NOT d.include_in_pipeline
                  AND EXISTS (
                      SELECT 1 FROM fact_fundamentals_us f WHERE f.cik = d.cik
                  )
                  AND primary_ticker IS NOT NULL
                  AND COALESCE(exchange, '') <> ''
                  AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                  AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
                  AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221', '6189')
            ),
            active_empty AS (
                SELECT d.cik,
                       row_number() OVER (ORDER BY d.cik DESC) AS rn
                FROM dim_company_us d
                WHERE d.include_in_pipeline
                  AND d.pipeline_sample_group = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM fact_fundamentals_us f WHERE f.cik = d.cik
                  )
            ),
            swaps AS (
                SELECT s.cik AS activate_cik, a.cik AS deactivate_cik
                FROM stale_parsed s
                JOIN active_empty a ON a.rn = s.rn
            ),
            deactivated AS (
                UPDATE dim_company_us d
                   SET include_in_pipeline = false,
                       pipeline_sample_group = NULL,
                       updated_at = now()
                  FROM swaps
                 WHERE d.cik = swaps.deactivate_cik
                RETURNING d.cik, d.primary_ticker, d.name
            )
            UPDATE dim_company_us d
               SET include_in_pipeline = true,
                   pipeline_sample_group = %s,
                   updated_at = now()
              FROM swaps
             WHERE d.cik = swaps.activate_cik
            RETURNING d.cik, d.primary_ticker, d.name
            """,
            (group, group),
        )
        recovered = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_us
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
                    SELECT cik
                    FROM dim_company_us
                    WHERE NOT include_in_pipeline
                      AND primary_ticker IS NOT NULL
                      AND COALESCE(exchange, '') <> ''
                      AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                      AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
                      AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221')
                    ORDER BY
                        CASE exchange
                            WHEN 'NYSE' THEN 1
                            WHEN 'Nasdaq' THEN 2
                            WHEN 'NYSE American' THEN 3
                            WHEN 'NYSE Arca' THEN 4
                            ELSE 5
                        END,
                        cik
                    LIMIT %s
                )
                UPDATE dim_company_us d
                   SET include_in_pipeline = true,
                       pipeline_sample_group = %s,
                       updated_at = now()
                  FROM candidates c
                 WHERE d.cik = c.cik
                RETURNING d.cik, d.primary_ticker, d.name
                """,
                (needed, group),
            )
            activated = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_us
            WHERE include_in_pipeline
              AND pipeline_sample_group = %s
            """,
            (group,),
        )
        active_after = cur.fetchone()[0]
    assert_us_master_quality()
    return {
        "group": group,
        "target": target,
        "active_before": active_before,
        "active_after": active_after,
        "recovered": [
            {"cik": row[0], "ticker": row[1], "name": row[2]}
            for row in recovered
        ],
        "activated": [
            {"cik": row[0], "ticker": row[1], "name": row[2]}
            for row in activated
        ],
    }


def activate_all_eligible_us_pipeline_scope(group: str = "us_all_eligible_20260524") -> dict[str, object]:
    """Activate all eligible US companies and remove stale ineligible actives."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM dim_company_us WHERE include_in_pipeline")
        active_before = cur.fetchone()[0]
        cur.execute(
            """
            UPDATE dim_company_us
               SET include_in_pipeline = false,
                   pipeline_sample_group = NULL,
                   updated_at = now()
             WHERE include_in_pipeline
               AND NOT (
                    primary_ticker IS NOT NULL
                AND COALESCE(exchange, '') <> ''
                AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
                AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221', '6189')
               )
            RETURNING cik, primary_ticker, name
            """
        )
        deactivated = cur.fetchall()
        cur.execute(
            """
            WITH candidates AS (
                SELECT cik
                FROM dim_company_us
                WHERE NOT include_in_pipeline
                  AND primary_ticker IS NOT NULL
                  AND COALESCE(exchange, '') <> ''
                  AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
                  AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
                  AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221', '6189')
            )
            UPDATE dim_company_us d
               SET include_in_pipeline = true,
                   pipeline_sample_group = COALESCE(d.pipeline_sample_group, %s),
                   updated_at = now()
              FROM candidates c
             WHERE d.cik = c.cik
            RETURNING d.cik, d.primary_ticker, d.name
            """,
            (group,),
        )
        activated = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM dim_company_us WHERE include_in_pipeline")
        active_after = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM dim_company_us
            WHERE primary_ticker IS NOT NULL
              AND COALESCE(exchange, '') <> ''
              AND UPPER(exchange) NOT IN ('OTC', 'OTC BULLETIN BOARD', 'PINK SHEETS')
              AND COALESCE(entity_class, '') NOT IN ('FUND', 'TRUST')
              AND COALESCE(sic, '') NOT IN ('6770', '6722', '6726', '6221', '6189')
            """
        )
        eligible = cur.fetchone()[0]
    assert_us_master_quality()
    return {
        "group": group,
        "eligible": eligible,
        "active_before": active_before,
        "active_after": active_after,
        "activated_count": len(activated),
        "deactivated_count": len(deactivated),
        "activated": [
            {"cik": row[0], "ticker": row[1], "name": row[2]}
            for row in activated[:50]
        ],
        "deactivated": [
            {"cik": row[0], "ticker": row[1], "name": row[2]}
            for row in deactivated[:50]
        ],
    }


def download_us_companyfacts(
    force: bool = False,
    max_ciks: int | None = None,
    entity_ids: list[str] | None = None,
    since: date | None = None,
    lookback_days: int | None = None,
) -> tuple[int, int]:
    ctx = start_run("US", "companyfacts_download", "full_refresh" if force else "incremental")
    try:
        _log_us("companyfacts_download", "start", force=force, max_ciks=max_ciks,
                entities=len(entity_ids) if entity_ids else None,
                since=since.isoformat() if since else None, lookback_days=lookback_days)
        stats = download_companyfacts_for_master(
            force=force,
            max_ciks=max_ciks,
            entity_ids=entity_ids,
            since=since,
            lookback_days=lookback_days,
            progress_callback=lambda payload: _record_source_progress(ctx, payload),
        )
        # ok = CIKs left in a good state (refreshed, new, or correctly skipped);
        # err = real failures only (SEC 404s are tracked separately).
        ok = stats["fetched"] + stats["new"] + stats["skipped_unchanged"]
        err = stats["errors"]
        finish_run(ctx, "succeeded", rows_in=stats["candidates"], rows_out=stats["fetched"] + stats["new"],
                   error=None if err == 0 else f"{err} errors")
        _log_us("companyfacts_download", "finished",
                fetched=stats["fetched"], new=stats["new"],
                skipped_unchanged=stats["skipped_unchanged"],
                not_found=stats["not_found"], errors=err, window=stats["window"])
        return ok, err
    except Exception as exc:
        _log_us("companyfacts_download", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def download_us_sources(
    force: bool = False,
    max_ciks: int | None = None,
    xbrl_limit: int | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    entity_ids: list[str] | None = None,
    since: date | None = None,
    lookback_days: int | None = None,
) -> dict[str, int]:
    """Download the mandatory composite US source set.

    The US raw fact table is parsed from SEC companyfacts JSON plus accession-
    matched filing XBRL ZIPs. Companyfacts provide the numeric fact stream;
    extracted XBRL linkbases provide hierarchy, calculation, and presentation
    metadata for those same accessions.
    """
    forms = _filing_type_filter(filing_types)
    ok, err = download_us_companyfacts(
        force=force, max_ciks=max_ciks, entity_ids=entity_ids, since=since, lookback_days=lookback_days
    )
    entity_scope = _entity_scope(entity_ids=entity_ids, max_ciks=max_ciks)
    indexed = sync_companyfacts_index(entity_scope)
    if max_ciks is None:
        prune_us_state_to_active_scope()
    sync_master_dimensions("US")
    local = reconcile_us_xbrl(entity_ids=entity_scope)
    xbrl = download_us_xbrl(entity_ids=entity_scope, force=force, limit=xbrl_limit, filing_types=forms)
    extracted = extract_us_xbrl(entity_ids=entity_scope, force=False)
    return {
        "companyfacts_downloaded": ok,
        "companyfacts_errors": err,
        "filings_indexed": indexed,
        "local_xbrl_zips": local["local_zips"],
        "xbrl_candidates": xbrl["candidates"],
        "xbrl_downloaded": xbrl["downloaded"],
        "xbrl_skipped": xbrl["skipped"],
        "xbrl_not_found": xbrl["not_found"],
        "xbrl_errors": xbrl["errors"],
        "linkbases_written": extracted["written"],
        "linkbases_missing": extracted["missing"],
        "linkbase_errors": extracted["errors"],
    }


def download_us_raw_sec_filings(
    force: bool = False,
    max_ciks: int | None = None,
    xbrl_limit: int | None = None,
    include_in_pipeline: bool = True,
    filed_date_from: str | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Raw SEC acquisition only: submissions index, XBRL ZIPs, linkbases/HTML.

    This intentionally does not call parse_us_raw, standardize, metrics, recon,
    or validation. It is for building the local on-disk raw filing corpus first.
    """
    forms = _current_period_forms(_filing_type_filter(filing_types))
    entity_scope = _entity_scope(max_ciks=max_ciks, include_in_pipeline=include_in_pipeline)
    ctx = start_run("US", "raw_sec_filing_acquisition", "full_refresh" if force else "incremental")
    try:
        submissions = sync_submissions_filing_index(
            entity_scope,
            force=force,
            include_in_pipeline=include_in_pipeline,
        )
        if max_ciks is None and include_in_pipeline:
            prune_us_state_to_active_scope()
        sync_master_dimensions("US")
        local = reconcile_us_xbrl(entity_ids=entity_scope)
        xbrl = download_us_xbrl_zips(
            entity_ids=entity_scope,
            force=force,
            limit=xbrl_limit,
            forms=forms,
            filed_date_from=filed_date_from,
            progress_callback=lambda payload: _record_source_progress(ctx, payload),
        )
        extracted = extract_us_xbrl(entity_ids=entity_scope, force=False)
        finish_run(
            ctx,
            "succeeded",
            rows_in=submissions["filings_indexed"],
            rows_out=xbrl["downloaded"] + extracted["written"],
            error=None if submissions["errors"] == 0 and xbrl["errors"] == 0 and extracted["errors"] == 0 else "raw acquisition completed with errors",
        )
        return {
            "companies": submissions["companies"],
            "submission_files": submissions["submission_files"],
            "submission_errors": submissions["errors"],
            "filings_indexed": submissions["filings_indexed"],
            "local_xbrl_zips": local["local_zips"],
            "xbrl_candidates": xbrl["candidates"],
            "xbrl_downloaded": xbrl["downloaded"],
            "xbrl_skipped": xbrl["skipped"],
            "xbrl_not_found": xbrl["not_found"],
            "xbrl_errors": xbrl["errors"],
            "linkbases_processed": extracted["processed"],
            "linkbases_written": extracted["written"],
            "linkbases_missing": extracted["missing"],
            "linkbase_errors": extracted["errors"],
        }
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def download_us_xbrl(
    entity_ids: list[str] | None = None,
    force: bool = False,
    limit: int | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> dict[str, int]:
    ctx = start_run("US", "xbrl_package_download", "full_refresh" if force else "incremental")
    try:
        scope = _entity_scope(entity_ids)
        forms = _filing_type_filter(filing_types)
        if entity_ids is None:
            prune_us_state_to_active_scope()
        result = download_us_xbrl_zips(
            entity_ids=scope,
            force=force,
            limit=limit,
            forms=forms,
            progress_callback=lambda payload: _record_source_progress(ctx, payload),
        )
        finish_run(ctx, "succeeded", rows_in=result["candidates"], rows_out=result["downloaded"])
        return result
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def reconcile_us_xbrl(entity_ids: list[str] | None = None) -> dict[str, int]:
    ctx = start_run("US", "xbrl_inventory_reconcile", "incremental")
    try:
        scope = _entity_scope(entity_ids)
        _log_us("xbrl_inventory_reconcile", "start", scope=len(scope))
        record_stage_event(
            ctx,
            "stage_started",
            f"US XBRL inventory reconcile started: scope={len(scope)}",
            {"phase": "started", "entity_scope_count": len(scope), "rows_in": 0, "rows_out": 0},
        )
        result = reconcile_local_xbrl_inventory(entity_ids=scope)
        update_run_progress(ctx, rows_in=result["candidates"], rows_out=result["local_zips"])
        record_stage_event(
            ctx,
            "stage_finished",
            (
                "US XBRL inventory reconcile finished: "
                f"candidates={result['candidates']} local_zips={result['local_zips']}"
            ),
            {"phase": "finished", "rows_in": result["candidates"], "rows_out": result["local_zips"], **result},
        )
        finish_run(ctx, "succeeded", rows_in=result["candidates"], rows_out=result["local_zips"])
        _log_us("xbrl_inventory_reconcile", "finished", candidates=result["candidates"], local_zips=result["local_zips"])
        return result
    except Exception as exc:
        _log_us("xbrl_inventory_reconcile", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def index_us_companyfacts(entity_ids: list[str] | None = None) -> int:
    ctx = start_run("US", "filing_index", "incremental")
    try:
        scope = _entity_scope(entity_ids)
        _log_us("filing_index", "start", scope=len(scope))

        def _progress(total: dict[str, int | str]) -> None:
            files = int(total.get("files") or 0)
            rows_seen = int(total.get("rows") or 0)
            update_run_progress(ctx, rows_in=files, rows_out=rows_seen)
            _log_us("filing_index", "progress", phase=total.get("phase"), files=files, rows=rows_seen)

        rows = sync_companyfacts_index(scope, progress_callback=_progress)
        if entity_ids is None:
            prune_us_state_to_active_scope()
        sync_master_dimensions("US")
        finish_run(ctx, "succeeded", rows_out=rows)
        _log_us("filing_index", "finished", rows=rows)
        return rows
    except Exception as exc:
        _log_us("filing_index", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def extract_us_xbrl(
    entity_ids: list[str] | None = None,
    force: bool = False,
    workers: int = 1,
    skip_existing_stems: bool = False,
) -> dict[str, int]:
    ctx = start_run("US", "linkbase_extract", "reparse" if force else "incremental")
    try:
        scope = _entity_scope(entity_ids)
        _log_us("linkbase_extract", "start", scope=len(scope), force=force)
        local = reconcile_local_xbrl_inventory(entity_ids=scope)

        def _progress(total: dict[str, int]) -> None:
            update_run_progress(ctx, rows_in=total["processed"], rows_out=total["written"])
            _log_us("linkbase_extract", "progress", processed=total["processed"], written=total["written"], missing=total["missing"], errors=total["errors"])

        result = extract_us_linkbases(
            entity_ids=scope,
            force=force,
            workers=workers,
            skip_existing_stems=skip_existing_stems,
            db_driven=not force,
            progress_callback=_progress,
        )
        sync_master_dimensions("US")
        result["local_xbrl_zips"] = local["local_zips"]
        finish_run(ctx, "succeeded", rows_in=result["processed"], rows_out=result["written"])
        _log_us("linkbase_extract", "finished", processed=result["processed"], written=result["written"], missing=result["missing"], errors=result["errors"])
        return result
    except Exception as exc:
        _log_us("linkbase_extract", "failed", error=str(exc))
        finish_run(ctx, "failed", error=str(exc))
        raise


def _max_filed(rows: list[dict]) -> date | None:
    dates = [r.get("filed_date") for r in rows if r.get("filed_date")]
    return max(dates) if dates else None


def _load_current_filing_periods(scope: list[str], filing_types: tuple[str, ...] | None = None) -> dict[str, date]:
    forms = _current_period_forms(filing_types)
    sql = """
        SELECT filing_id, period_end
          FROM source_filing_state
         WHERE jurisdiction = 'US'
           AND entity_id = ANY(%s)
           AND period_end IS NOT NULL
           AND EXISTS (
               SELECT 1
               FROM regexp_split_to_table(filing_type, ',') AS form(form_name)
               WHERE btrim(form.form_name) = ANY(%s)
           )
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (list(scope), list(forms)))
        return {row[0]: row[1] for row in cur.fetchall()}


def parse_us_raw(
    entity_ids: list[str] | None = None,
    force: bool = False,
    ensure_linkbases: bool = True,
    annual_10k_current_only: bool = True,
    sync_index: bool = True,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> int:
    mode = "reparse" if force else "incremental"
    ctx = start_run("US", "raw_parse", mode)
    total_rows = 0
    total_files = 0
    agg_stats = {"kept": 0}
    try:
        scope = _entity_scope(entity_ids)
        forms = _filing_type_filter(filing_types)
        _log_us(
            "raw_parse",
            "start",
            scope=len(scope),
            force=force,
            ensure_linkbases=ensure_linkbases,
            annual_10k_current_only=annual_10k_current_only,
            filing_types=",".join(forms) if forms else None,
            sync_index=sync_index,
        )
        if sync_index:
            _log_us("raw_parse", "companyfacts index start", scope=len(scope))

            def _index_progress(total: dict[str, int | str]) -> None:
                files = int(total.get("files") or 0)
                rows_seen = int(total.get("rows") or 0)
                update_run_progress(ctx, rows_in=files, rows_out=rows_seen)
                _log_us("raw_parse", "companyfacts index progress", phase=total.get("phase"), files=files, rows=rows_seen)

            indexed = sync_companyfacts_index(scope, progress_callback=_index_progress)
            _log_us("raw_parse", "companyfacts index finished", rows=indexed)
        else:
            _log_us("raw_parse", "companyfacts index skipped")
        if entity_ids is None:
            _log_us("raw_parse", "prune active scope start")
            prune_us_state_to_active_scope()
            _log_us("raw_parse", "prune active scope finished")
        _log_us("raw_parse", "master dimension sync start")
        sync_master_dimensions("US")
        _log_us("raw_parse", "master dimension sync finished")
        if ensure_linkbases:
            _log_us("raw_parse", "ensure linkbases start")
            reconcile_us_xbrl(entity_ids=scope)
            extract_us_xbrl(entity_ids=scope, force=False)
            _log_us("raw_parse", "ensure linkbases finished")

        def _candidate_progress(total: dict[str, int | str]) -> None:
            files_seen = int(total.get("files") or 0)
            rows_seen = int(total.get("rows") or 0)
            update_run_progress(ctx, rows_in=files_seen, rows_out=rows_seen)
            _log_us("raw_parse", "candidate progress", phase=total.get("phase"), files=files_seen, candidates=rows_seen)

        _log_us("raw_parse", "candidate selection start")
        files = changed_or_unparsed_files(scope, force=force, progress_callback=_candidate_progress)
        _log_us("raw_parse", "candidate selection finished", candidates=len(files))
        _log_us("raw_parse", "current filing periods start", enabled=annual_10k_current_only)
        current_filing_periods = _load_current_filing_periods(scope, forms) if annual_10k_current_only else None
        _log_us("raw_parse", "current filing periods finished", periods=len(current_filing_periods or {}))
        for item in files:
            parsed_rows, stats = parse_companyfacts_file(
                item,
                annual_10k_periods=current_filing_periods,
                filing_types=frozenset(forms) if forms else None,
            )
            for k, v in stats.items():
                agg_stats[k] = agg_stats.get(k, 0) + v
            rows_out = upsert_us_facts(parsed_rows) if parsed_rows else 0
            total_rows += rows_out
            total_files += 1
            filing_ids = {r.get("filing_id") for r in parsed_rows if r.get("filing_id")}
            mark_source_entity_filings_parsed("US", item.cik, source_hash=item.source_hash, parsed=True)
            mark_source_filings_parsed("US", filing_ids, parsed=True)
            record_entity_state(
                ctx,
                entity_id=item.cik,
                source_hash=item.source_hash,
                max_filed_date=_max_filed(parsed_rows),
                rows_in=len(parsed_rows),
                rows_out=rows_out,
                status="succeeded",
            )
            if total_files == 1 or total_files % 25 == 0:
                update_run_progress(ctx, rows_in=total_files, rows_out=total_rows)
                _log_us("raw_parse", "parse progress", files=total_files, rows=total_rows, last_cik=item.cik, last_rows=rows_out)
        print(f"us_raw_parse_drop_stats: {agg_stats}", flush=True)
        finish_run(ctx, "succeeded", rows_in=total_files, rows_out=total_rows)
        _log_us("raw_parse", "finished", files=total_files, rows=total_rows)
        return total_rows
    except Exception as exc:
        _log_us("raw_parse", "failed", files=total_files, rows=total_rows, error=str(exc))
        finish_run(ctx, "failed", rows_in=total_files, rows_out=total_rows, error=str(exc))
        raise


def reparse(
    entity_ids: list[str] | None = None,
    annual_10k_current_only: bool = True,
    filing_types: list[str] | tuple[str, ...] | None = None,
) -> None:
    scope = _entity_scope(entity_ids)
    forms = _filing_type_filter(filing_types)
    _log_us(
        "reparse",
        "start",
        scope=len(scope),
        annual_10k_current_only=annual_10k_current_only,
        filing_types=",".join(forms) if forms else None,
    )
    reset_downstream("US", None if entity_ids is None else scope)
    if entity_ids is None:
        prune_us_state_to_active_scope()
    sync_master_dimensions("US")
    reconcile_us_xbrl(entity_ids=scope)
    extract_us_xbrl(entity_ids=scope, force=False)
    parse_us_raw(
        entity_ids=scope,
        force=True,
        ensure_linkbases=False,
        annual_10k_current_only=annual_10k_current_only,
        filing_types=forms,
    )
    _log_us("standardize", "start", scope=len(scope), full=entity_ids is None)
    populate_us_std(entity_ids=entity_ids, full=entity_ids is None)
    _log_us("standardize", "finished")
    _log_us("metrics", "start", scope=len(scope), full=entity_ids is None)
    compute_metrics("US", entity_ids=entity_ids, full=entity_ids is None)
    _log_us("metrics", "finished")
    _log_us("recon", "start", scope=len(scope), full=entity_ids is None)
    build_recon("US", entity_ids=entity_ids, full=entity_ids is None)
    _log_us("recon", "finished")
    _log_us("reparse", "finished")


def run_incremental(
    download: bool = False,
    max_ciks: int | None = None,
    filing_types: list[str] | tuple[str, ...] | None = None,
    entity_ids: list[str] | None = None,
    since: date | None = None,
    lookback_days: int | None = None,
) -> int:
    if not source_dir().exists():
        raise FileNotFoundError(source_dir())
    forms = _filing_type_filter(filing_types)
    _log_us("pipeline", "start", download=download, max_ciks=max_ciks, filing_types=",".join(forms) if forms else None)
    refresh_us_master(download=download, full=False, max_ciks=max_ciks)
    entity_scope = _entity_scope(entity_ids=entity_ids, max_ciks=max_ciks)
    if download:
        download_us_sources(
            force=False, max_ciks=max_ciks, filing_types=forms,
            entity_ids=entity_ids, since=since, lookback_days=lookback_days,
        )
    else:
        index_us_companyfacts(entity_scope)
        reconcile_us_xbrl(entity_ids=entity_scope)
        extract_us_xbrl(entity_ids=entity_scope, force=False)
    rows = parse_us_raw(
        entity_ids=entity_scope,
        force=False,
        ensure_linkbases=False,
        sync_index=False,
        filing_types=forms,
    )
    _log_us("standardize", "start", scope=len(entity_scope), full=False)
    populate_us_std(entity_ids=entity_scope, full=False)
    _log_us("standardize", "finished")
    _log_us("metrics", "start", scope=len(entity_scope), full=False)
    compute_metrics("US", entity_ids=entity_scope, full=False)
    _log_us("metrics", "finished")
    _log_us("recon", "start", scope=len(entity_scope), full=False)
    build_recon("US", entity_ids=entity_scope, full=False)
    _log_us("recon", "finished")
    _log_us("pipeline", "finished", rows=rows)
    return rows
