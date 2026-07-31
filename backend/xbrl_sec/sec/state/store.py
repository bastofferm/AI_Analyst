"""Pipeline state helpers with explicit reset semantics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from typing import Iterable
from uuid import UUID, uuid4

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect


@dataclass(frozen=True)
class RunContext:
    run_id: UUID
    jurisdiction: str
    stage: str
    mode: str
    app_run_id: str | None = None


def start_run(jurisdiction: str, stage: str, mode: str, scope: str = "{}") -> RunContext:
    app_run_id = os.environ.get("MZQA_PIPELINE_APP_RUN_ID")
    ctx = RunContext(uuid4(), jurisdiction, stage, mode, app_run_id)
    if app_run_id:
        try:
            payload = json.loads(scope or "{}")
            if not isinstance(payload, dict):
                payload = {"scope": payload}
        except json.JSONDecodeError:
            payload = {"scope": scope}
        payload.setdefault("app_run_id", app_run_id)
        scope = json.dumps(payload, default=str)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_stage_run (run_id, jurisdiction, stage, mode, status, scope)
            VALUES (%s, %s, %s, %s, 'running', %s::jsonb)
            """,
            (str(ctx.run_id), jurisdiction, stage, mode, scope),
        )
    return ctx


def record_stage_event(
    ctx: RunContext,
    event_type: str,
    message: str,
    payload: dict | None = None,
    entity_id: str | None = None,
) -> None:
    """Attach structured progress detail to an app-launched pipeline stage."""
    if not ctx.app_run_id:
        return
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_app_event
                (app_run_id, event_type, stage_run_id, jurisdiction, stage, entity_id, message, payload)
            VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                ctx.app_run_id,
                event_type,
                str(ctx.run_id),
                ctx.jurisdiction,
                ctx.stage,
                entity_id,
                message,
                json.dumps(payload or {}, default=str),
            ),
        )


def finish_run(ctx: RunContext, status: str, rows_in: int = 0, rows_out: int = 0, error: str | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pipeline_stage_run
               SET status=%s, rows_in=%s, rows_out=%s, error_message=%s, finished_at=now()
             WHERE run_id=%s
            """,
            (status, rows_in, rows_out, error, str(ctx.run_id)),
        )


def update_run_progress(ctx: RunContext, rows_in: int = 0, rows_out: int = 0) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pipeline_stage_run
               SET rows_in=%s, rows_out=%s
             WHERE run_id=%s
            """,
            (rows_in, rows_out, str(ctx.run_id)),
        )


def record_entity_state(
    ctx: RunContext,
    entity_id: str,
    source_hash: str | None,
    max_filed_date: date | None,
    rows_in: int,
    rows_out: int,
    status: str = "succeeded",
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_entity_state
                (jurisdiction, entity_id, stage, source_hash, max_filed_date,
                 rows_in, rows_out, status, updated_at, last_run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (jurisdiction, entity_id, stage) DO UPDATE SET
                source_hash = EXCLUDED.source_hash,
                max_filed_date = EXCLUDED.max_filed_date,
                rows_in = EXCLUDED.rows_in,
                rows_out = EXCLUDED.rows_out,
                status = EXCLUDED.status,
                updated_at = now(),
                last_run_id = EXCLUDED.last_run_id
            """,
            (
                ctx.jurisdiction, entity_id, ctx.stage, source_hash, max_filed_date,
                rows_in, rows_out, status, str(ctx.run_id),
            ),
        )


def mark_source_filings_parsed(jurisdiction: str, filing_ids: Iterable[str], parsed: bool = True, error: str | None = None) -> int:
    ids = [v for v in filing_ids if v]
    if not ids:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_filing_state
               SET parsed=%s, parse_error=%s, updated_at=now()
             WHERE jurisdiction=%s AND filing_id = ANY(%s)
            """,
            (parsed, error, jurisdiction, ids),
        )
        return cur.rowcount


def update_source_filing_payload(jurisdiction: str, filing_id: str, payload: dict) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE source_filing_state
               SET raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s::jsonb,
                   updated_at = now()
             WHERE jurisdiction=%s AND filing_id=%s
            """,
            (json.dumps(payload, ensure_ascii=False), jurisdiction, filing_id),
        )


def mark_source_entity_filings_parsed(
    jurisdiction: str,
    entity_id: str,
    source_hash: str | None = None,
    parsed: bool = True,
    error: str | None = None,
) -> int:
    params = [parsed, error, jurisdiction, entity_id]
    hash_filter = ""
    if source_hash is not None:
        hash_filter = "AND source_hash = %s"
        params.append(source_hash)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE source_filing_state
               SET parsed=%s, parse_error=%s, updated_at=now()
             WHERE jurisdiction=%s
               AND entity_id=%s
               {hash_filter}
            """,
            params,
        )
        return cur.rowcount


def reset_downstream(jurisdiction: str, entity_ids: Iterable[str] | None = None) -> None:
    """Delete downstream state for a scoped reparse.

    This intentionally resets raw facts, std/metrics/recon placeholders, and state in
    one dependency-ordered place. The std/metrics/recon tables will be added as the
    new layer grows; the contract belongs here from day one.
    """
    ids = list(entity_ids or [])
    id_col = "cik" if jurisdiction == "US" else "edinet_code"
    fact_table = "fact_fundamentals_us" if jurisdiction == "US" else "fact_fundamentals_jp"
    std_table = "fact_fundamentals_std_us" if jurisdiction == "US" else "fact_fundamentals_std_jp"
    metrics_table = "fact_metrics_us" if jurisdiction == "US" else "fact_metrics_jp"
    recon_table = "fact_metrics_recon_us" if jurisdiction == "US" else "fact_metrics_recon_jp"
    with connect() as conn, conn.cursor() as cur:
        if ids:
            cur.execute(f"DELETE FROM {recon_table} WHERE {id_col} = ANY(%s)", (ids,))
            cur.execute(f"DELETE FROM {metrics_table} WHERE {id_col} = ANY(%s)", (ids,))
            cur.execute(f"DELETE FROM {std_table} WHERE {id_col} = ANY(%s)", (ids,))
            cur.execute(f"DELETE FROM {fact_table} WHERE {id_col} = ANY(%s)", (ids,))
            cur.execute(
                "DELETE FROM pipeline_entity_state WHERE jurisdiction=%s AND entity_id = ANY(%s)",
                (jurisdiction, ids),
            )
            cur.execute(
                "UPDATE source_filing_state SET parsed=FALSE, parse_error=NULL WHERE jurisdiction=%s AND entity_id = ANY(%s)",
                (jurisdiction, ids),
            )
        else:
            cur.execute(f"TRUNCATE {recon_table}")
            cur.execute(f"TRUNCATE {metrics_table}")
            cur.execute(f"TRUNCATE {std_table}")
            cur.execute(f"TRUNCATE {fact_table}")
            cur.execute("DELETE FROM pipeline_entity_state WHERE jurisdiction=%s", (jurisdiction,))
            cur.execute(
                "UPDATE source_filing_state SET parsed=FALSE, parse_error=NULL WHERE jurisdiction=%s",
                (jurisdiction,),
            )
