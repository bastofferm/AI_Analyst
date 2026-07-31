from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import acquire
from ..pipeline_catalog import build_cli_argv, command_catalog
from ..settings import get_settings


router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs" / "pipeline_app"
ORCHESTRATION_SQL = ROOT / "xbrl_sec" / "sec" / "sql" / "092_pipeline_app_orchestration.sql"

_LIVE_PROCS: dict[str, subprocess.Popen] = {}
_SCHEMA_READY = False


class LaunchRunRequest(BaseModel):
    command_key: str
    label: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class StagingRequest(BaseModel):
    operation: Literal[
        "source_parsed_reset",
        "us_xbrl_status_reset",
        "market_item_reset",
        "pipeline_scope_set",
    ]
    jurisdiction: Literal["US", "JP"] | None = None
    entity_ids: list[str] = Field(default_factory=list)
    source_kind: str | None = None
    filed_date_from: date | None = None
    filed_date_to: date | None = None
    statuses: list[str] = Field(default_factory=list)
    target_status: str = "pending"
    source: str | None = None
    source_keys: list[str] = Field(default_factory=list)
    clear_hash: bool = False
    include_in_pipeline: bool | None = None
    sample_group: str | None = None


class ScopeProfileRequest(BaseModel):
    jurisdiction: Literal["US", "JP"]
    name: str
    description: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    sample_group: str | None = None


class ScopeProfileApplyRequest(BaseModel):
    include_in_pipeline: bool = True
    deactivate_other_entities: bool = False
    sample_group: str | None = None


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "hex") and value.__class__.__name__ == "UUID":
        return str(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return _jsonable(value)


def _record(row: Any) -> dict[str, Any]:
    data = {key: _jsonable(row[key]) for key in row.keys()}
    for key in ("params", "argv", "payload", "filters", "scope"):
        if key in data:
            data[key] = _decode_json(row[key])
    return data


def _affected_count(tag: str) -> int:
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0


def _clean_text_list(values: list[str] | None) -> list[str]:
    return [value.strip() for value in values or [] if value and value.strip()]


async def _ensure_orchestration_schema(conn) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    exists = await conn.fetchval("SELECT to_regclass('sec.pipeline_app_run')")
    if not exists:
        await conn.execute(ORCHESTRATION_SQL.read_text(encoding="utf-8"))
    _SCHEMA_READY = True


def _catalog_categories(commands: list[dict[str, Any]]) -> list[dict[str, str]]:
    labels = {
        "admin": "Admin",
        "fundamentals": "Fundamentals",
        "yahoo_global": "Yahoo Global",
        "market": "Market Data",
        "risk": "Risk + Factors",
    }
    seen: list[str] = []
    for command in commands:
        category = command["category"]
        if category not in seen:
            seen.append(category)
    return [{"key": key, "label": labels.get(key, key.title())} for key in seen]


def _jurisdiction_from_params(params: dict[str, Any]) -> str | None:
    value = params.get("jurisdiction")
    if isinstance(value, str) and value.upper() in {"US", "JP", "ALL"}:
        return value.upper()
    return None


def _spawn_process(app_run_id: str, argv: list[str], stdout_path: Path, stderr_path: Path) -> subprocess.Popen:
    settings = get_settings()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["MZQA_ROOT"] = str(ROOT)
    env["XBRL_SEC_DATABASE_URL"] = settings.database_url
    env["XBRL_SEC_SCHEMA"] = settings.db_schema
    env.setdefault("XBRL_SEC_MARKET_DATA_ROOT", r"D:\market_data")
    env["MZQA_PIPELINE_APP_RUN_ID"] = app_run_id
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # DETACHED_PROCESS: no console association, so the child cannot receive
        #   CTRL_CLOSE_EVENT when the parent's console is torn down. Without
        #   this, restarting uvicorn killed long-running pipeline subprocesses
        #   via the Intel Fortran runtime's window-CLOSE handler.
        # CREATE_NEW_PROCESS_GROUP: still useful so the group is independent.
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        creationflags = 0
    start_new_session = os.name != "nt"
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        return subprocess.Popen(
            argv,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
        )


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        os.kill(pid, signal.SIGTERM)
    return True


async def _insert_event(
    conn,
    app_run_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO sec.pipeline_app_event (app_run_id, event_type, message, payload)
        VALUES ($1::uuid, $2, $3, $4::jsonb)
        """,
        app_run_id,
        event_type,
        message,
        json.dumps(payload or {}, default=str),
    )


async def _fail_running_child_stages(conn, app_run_id: str, message: str) -> None:
    await conn.execute(
        """
        UPDATE sec.pipeline_stage_run
           SET status='failed',
               finished_at=COALESCE(finished_at, now()),
               error_message=COALESCE(error_message, $2)
         WHERE status='running'
           AND scope->>'app_run_id'=$1
        """,
        app_run_id,
        message[:4000],
    )
    await conn.execute(
        """
        UPDATE sec.market_source_item_state item
           SET status='failed',
               finished_at=COALESCE(item.finished_at, now()),
               error_message=COALESCE(item.error_message, $2),
               updated_at=now()
          FROM sec.pipeline_stage_run stage
         WHERE item.run_id=stage.run_id
           AND item.status='running'
           AND stage.scope->>'app_run_id'=$1
        """,
        app_run_id,
        message[:4000],
    )


async def _child_stage_summary(conn, app_run_id: str) -> Any:
    return await conn.fetchrow(
        """
        SELECT COUNT(*)::int AS total,
               COUNT(*) FILTER (WHERE status='running')::int AS running,
               COUNT(*) FILTER (WHERE status='failed')::int AS failed,
               COUNT(*) FILTER (WHERE status='succeeded')::int AS succeeded,
               COALESCE(SUM(rows_out), 0)::bigint AS rows_out
          FROM sec.pipeline_stage_run
         WHERE scope->>'app_run_id'=$1
        """,
        app_run_id,
    )


async def _infer_finished_app_status(
    conn,
    app_run_id: str,
    exit_code: int | None,
) -> tuple[str, int, str] | None:
    if exit_code is not None:
        if exit_code == 0:
            return ("succeeded", 0, "Process exited with code 0.")
        return ("failed", exit_code, f"Process exited with code {exit_code}.")

    summary = await _child_stage_summary(conn, app_run_id)
    if not summary or not summary["total"] or summary["running"]:
        return None
    if summary["failed"]:
        return ("failed", 1, "One or more child pipeline stages failed after the parent process exited.")
    if summary["succeeded"]:
        return ("succeeded", 0, "Child pipeline stages completed successfully after the parent process exited.")
    return None


async def _set_finished_app_status(
    conn,
    app_run_id: str,
    status: str,
    exit_code: int,
    message: str,
) -> Any:
    await conn.execute(
        """
        UPDATE sec.pipeline_app_run
           SET status=$2,
               exit_code=COALESCE(exit_code, $3),
               finished_at=COALESCE(finished_at, now()),
               updated_at=now(),
               error_message=CASE
                   WHEN $2='succeeded' THEN NULL
                   ELSE COALESCE(error_message, $4)
               END
         WHERE app_run_id=$1::uuid
        """,
        app_run_id,
        status,
        exit_code,
        message[:4000],
    )
    if status == "failed":
        await _fail_running_child_stages(conn, app_run_id, message)
    await _insert_event(conn, app_run_id, status, message, {"exit_code": exit_code, "reconciled": True})
    return await conn.fetchrow("SELECT * FROM sec.pipeline_app_run WHERE app_run_id=$1::uuid", app_run_id)


async def _refresh_app_run(conn, row: Any) -> Any:
    if not row:
        return row

    app_run_id = str(row["app_run_id"])
    if row["status"] == "unknown":
        inferred = await _infer_finished_app_status(conn, app_run_id, row["exit_code"])
        if inferred:
            return await _set_finished_app_status(conn, app_run_id, *inferred)
        return row
    if row["status"] != "running":
        return row

    proc = _LIVE_PROCS.get(app_run_id)
    exit_code: int | None = None

    if proc:
        exit_code = proc.poll()
        if exit_code is None:
            return row
        _LIVE_PROCS.pop(app_run_id, None)
    else:
        try:
            alive = _pid_is_running(row["process_id"])
        except Exception:
            # tasklist transient failure (timeout, OS hiccup under load).
            # Don't conclude the process is gone — leave the row unchanged
            # and re-check on the next poll. Avoids false "failed" markings.
            return row
        if alive:
            return row
        message = "Process is no longer visible to the API process."
        inferred = await _infer_finished_app_status(conn, app_run_id, row["exit_code"])
        if inferred:
            return await _set_finished_app_status(conn, app_run_id, *inferred)
        summary = await _child_stage_summary(conn, app_run_id)
        if summary and summary["running"]:
            return await _set_finished_app_status(
                conn,
                app_run_id,
                "failed",
                1,
                "Process disappeared while child pipeline stages were still running.",
            )
        await conn.execute(
            """
            UPDATE sec.pipeline_app_run
               SET status='unknown', finished_at=now(), updated_at=now(),
                   error_message=COALESCE(error_message, $2)
             WHERE app_run_id=$1::uuid
            """,
            app_run_id,
            message,
        )
        await _fail_running_child_stages(conn, app_run_id, message)
        await _insert_event(conn, app_run_id, "status_unknown", "Process disappeared before the API observed an exit code.")
        return await conn.fetchrow("SELECT * FROM sec.pipeline_app_run WHERE app_run_id=$1::uuid", app_run_id)

    status = "succeeded" if exit_code == 0 else "failed"
    return await _set_finished_app_status(conn, app_run_id, status, exit_code, f"Process exited with code {exit_code}.")


async def _get_app_run(conn, app_run_id: str) -> Any:
    row = await conn.fetchrow("SELECT * FROM sec.pipeline_app_run WHERE app_run_id=$1::uuid", app_run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Pipeline app run not found")
    return await _refresh_app_run(conn, row)


async def _fact_counts(conn) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT 'US' AS jurisdiction, 'raw' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_fundamentals_us GROUP BY fiscal_period
        UNION ALL
        SELECT 'JP' AS jurisdiction, 'raw' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_fundamentals_jp GROUP BY fiscal_period
        UNION ALL
        SELECT 'US' AS jurisdiction, 'std' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_fundamentals_std_us GROUP BY fiscal_period
        UNION ALL
        SELECT 'JP' AS jurisdiction, 'std' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_fundamentals_std_jp GROUP BY fiscal_period
        UNION ALL
        SELECT 'US' AS jurisdiction, 'metrics' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_metrics_us GROUP BY fiscal_period
        UNION ALL
        SELECT 'JP' AS jurisdiction, 'metrics' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_metrics_jp GROUP BY fiscal_period
        UNION ALL
        SELECT 'US' AS jurisdiction, 'recon' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_metrics_recon_us GROUP BY fiscal_period
        UNION ALL
        SELECT 'JP' AS jurisdiction, 'recon' AS layer, fiscal_period, COUNT(*)::bigint AS rows
          FROM sec.fact_metrics_recon_jp GROUP BY fiscal_period
        ORDER BY jurisdiction, layer, fiscal_period
        """
    )
    return [_record(row) for row in rows]


@router.get("/catalog")
async def catalog() -> dict[str, Any]:
    commands = command_catalog()
    presets = [
        {
            "key": "daily.us_incremental",
            "label": "US Daily Incremental",
            "commands": [{"command_key": "fundamentals.run", "params": {"jurisdiction": "US", "download": True}}],
        },
        {
            "key": "daily.jp_incremental",
            "label": "JP Daily Incremental",
            "commands": [{"command_key": "fundamentals.run", "params": {"jurisdiction": "JP", "download": True}}],
        },
        {
            "key": "daily.market_data",
            "label": "Market Data Daily",
            "commands": [
                {"command_key": "market.fetch_prices", "params": {}},
                {"command_key": "market.fetch_stock_splits", "params": {"jurisdiction": "ALL"}},
                {"command_key": "market.fetch_fama_french", "params": {}},
            ],
        },
        {
            "key": "daily.risk_model",
            "label": "Risk Model Daily",
            "commands": [
                {"command_key": "risk.compute_factor_model", "params": {"workers": 4, "chunk_size": 50}},
            ],
        },
        {
            "key": "quarterly.fundamentals_quality_refresh",
            "label": "Quarterly Fundamentals Quality Refresh",
            "cadence": "quarterly",
            "description": (
                "Advisory-first staged refresh for new SEC/EDINET filings, standardized facts, "
                "metrics, recon traces and validation. Uses incremental commands only."
            ),
            "commands": [
                {
                    "command_key": "fundamentals.run",
                    "params": {"jurisdiction": "US", "download": True, "lookback_days": 120},
                },
                {"command_key": "fundamentals.validate", "params": {"jurisdiction": "US"}},
                {
                    "command_key": "fundamentals.run",
                    "params": {"jurisdiction": "JP", "download": True},
                },
                {"command_key": "fundamentals.validate", "params": {"jurisdiction": "JP"}},
            ],
        },
    ]
    return {"commands": commands, "categories": _catalog_categories(commands), "presets": presets}


@router.post("/runs")
async def launch_run(request: LaunchRunRequest) -> dict[str, Any]:
    try:
        command, cli_args = build_cli_argv(request.command_key, request.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    app_run_id = str(uuid4())
    stdout_path = LOG_DIR / f"{app_run_id}.stdout.log"
    stderr_path = LOG_DIR / f"{app_run_id}.stderr.log"
    argv = [sys.executable, "-m", "xbrl_sec.sec.cli", *cli_args]
    label = request.label or command.label
    jurisdiction = _jurisdiction_from_params(request.params)

    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        await conn.execute(
            """
            INSERT INTO sec.pipeline_app_run
                (app_run_id, label, category, command_key, jurisdiction, params, argv,
                 status, cwd, stdout_path, stderr_path)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb,
                    'queued', $8, $9, $10)
            """,
            app_run_id,
            label,
            command.category,
            request.command_key,
            jurisdiction,
            json.dumps(request.params, default=str),
            json.dumps(argv, default=str),
            str(ROOT),
            str(stdout_path),
            str(stderr_path),
        )
        await _insert_event(conn, app_run_id, "queued", "Run accepted by app; launching process.", {"argv": argv})

    try:
        proc = _spawn_process(app_run_id, argv, stdout_path, stderr_path)
    except Exception as exc:
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE sec.pipeline_app_run
                   SET status='failed', error_message=$2, finished_at=now(), updated_at=now()
                 WHERE app_run_id=$1::uuid
                """,
                app_run_id,
                f"{exc.__class__.__name__}: {exc}",
            )
            await _insert_event(conn, app_run_id, "failed_to_start", "Process failed to start.", {"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Failed to start pipeline command: {exc}") from exc

    _LIVE_PROCS[app_run_id] = proc
    async with acquire() as conn:
        await conn.execute(
            """
            UPDATE sec.pipeline_app_run
               SET status='running', process_id=$2, updated_at=now()
             WHERE app_run_id=$1::uuid
            """,
            app_run_id,
            proc.pid,
        )
        await _insert_event(conn, app_run_id, "started", "Process started.", {"pid": proc.pid})
        row = await conn.fetchrow("SELECT * FROM sec.pipeline_app_run WHERE app_run_id=$1::uuid", app_run_id)
    return {"run": _record(row)}


@router.get("/runs")
async def list_runs(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=250),
) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        if status:
            rows = await conn.fetch(
                """
                SELECT * FROM sec.pipeline_app_run
                 WHERE status=$1
                 ORDER BY started_at DESC
                 LIMIT $2
                """,
                status,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM sec.pipeline_app_run ORDER BY started_at DESC LIMIT $1",
                limit,
            )
        refreshed = [await _refresh_app_run(conn, row) for row in rows]
    return {"runs": [_record(row) for row in refreshed]}


@router.get("/runs/{app_run_id}")
async def get_run(app_run_id: str) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        run = await _get_app_run(conn, app_run_id)
        stage_rows = await conn.fetch(
            """
            SELECT * FROM sec.pipeline_stage_run
             WHERE scope->>'app_run_id'=$1
             ORDER BY started_at DESC
            """,
            app_run_id,
        )
        entity_progress = await conn.fetch(
            """
            WITH stage_ids AS (
                SELECT run_id FROM sec.pipeline_stage_run WHERE scope->>'app_run_id'=$1
            )
            SELECT jurisdiction, stage, status, COUNT(*)::bigint AS entities,
                   COALESCE(SUM(rows_in), 0)::bigint AS rows_in,
                   COALESCE(SUM(rows_out), 0)::bigint AS rows_out,
                   MAX(updated_at) AS updated_at
              FROM sec.pipeline_entity_state
             WHERE last_run_id IN (SELECT run_id FROM stage_ids)
             GROUP BY jurisdiction, stage, status
             ORDER BY jurisdiction, stage, status
            """,
            app_run_id,
        )
        latest_entities = await conn.fetch(
            """
            WITH stage_ids AS (
                SELECT run_id FROM sec.pipeline_stage_run WHERE scope->>'app_run_id'=$1
            )
            SELECT jurisdiction, entity_id, stage, status, rows_in, rows_out,
                   max_filed_date, updated_at
              FROM sec.pipeline_entity_state
             WHERE last_run_id IN (SELECT run_id FROM stage_ids)
             ORDER BY updated_at DESC
             LIMIT 40
            """,
            app_run_id,
        )
        market_progress = await conn.fetch(
            """
            WITH stage_ids AS (
                SELECT run_id FROM sec.pipeline_stage_run WHERE scope->>'app_run_id'=$1
            )
            SELECT source, status, COUNT(*)::bigint AS items,
                   COALESCE(SUM(rows_in), 0)::bigint AS rows_in,
                   COALESCE(SUM(rows_out), 0)::bigint AS rows_out,
                   MAX(updated_at) AS updated_at
              FROM sec.market_source_item_state
             WHERE run_id IN (SELECT run_id FROM stage_ids)
             GROUP BY source, status
             ORDER BY source, status
            """,
            app_run_id,
        )
        events = await conn.fetch(
            """
            SELECT * FROM sec.pipeline_app_event
             WHERE app_run_id=$1::uuid
             ORDER BY created_at DESC
             LIMIT 80
            """,
            app_run_id,
        )
    return {
        "run": _record(run),
        "stages": [_record(row) for row in stage_rows],
        "entity_progress": [_record(row) for row in entity_progress],
        "latest_entities": [_record(row) for row in latest_entities],
        "market_progress": [_record(row) for row in market_progress],
        "events": [_record(row) for row in events],
    }


@router.get("/runs/{app_run_id}/logs")
async def get_run_logs(
    app_run_id: str,
    stream: Literal["stdout", "stderr"] = Query(default="stdout"),
    tail: int = Query(default=120_000, ge=1, le=2_000_000),
) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        run = await _get_app_run(conn, app_run_id)
    path = Path(run["stdout_path"] if stream == "stdout" else run["stderr_path"])
    safe_root = LOG_DIR.resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid log path: {exc}") from exc
    if safe_root not in resolved.parents and resolved != safe_root:
        raise HTTPException(status_code=400, detail="Log path is outside the pipeline_app log directory")
    if not resolved.exists():
        return {"stream": stream, "content": "", "path": str(resolved), "bytes": 0}
    size = resolved.stat().st_size
    with resolved.open("rb") as handle:
        handle.seek(max(size - tail, 0))
        content = handle.read().decode("utf-8", errors="replace")
    return {"stream": stream, "content": content, "path": str(resolved), "bytes": size}


@router.post("/runs/{app_run_id}/cancel")
async def cancel_run(app_run_id: str) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        row = await _get_app_run(conn, app_run_id)
        pid = row["process_id"]
        proc = _LIVE_PROCS.pop(app_run_id, None)
        killed = _terminate_pid(pid)
        if proc and proc.poll() is None and not killed:
            proc.terminate()
            killed = True
        await conn.execute(
            """
            UPDATE sec.pipeline_app_run
               SET status='cancelled', finished_at=now(), updated_at=now(),
                   error_message=COALESCE(error_message, 'Cancelled from dataPipelineApp.')
             WHERE app_run_id=$1::uuid
            """,
            app_run_id,
        )
        await _fail_running_child_stages(conn, app_run_id, "Parent app run was cancelled from dataPipelineApp.")
        await _insert_event(conn, app_run_id, "cancelled", "Run cancelled from dataPipelineApp.", {"pid": pid, "killed": killed})
        updated = await conn.fetchrow("SELECT * FROM sec.pipeline_app_run WHERE app_run_id=$1::uuid", app_run_id)
    return {"run": _record(updated), "killed": killed}


@router.get("/overview")
async def overview(include_counts: bool = Query(default=False)) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        app_rows = await conn.fetch("SELECT * FROM sec.pipeline_app_run ORDER BY started_at DESC LIMIT 30")
        app_rows = [await _refresh_app_run(conn, row) for row in app_rows]
        stage_rows = await conn.fetch(
            """
            SELECT * FROM sec.pipeline_stage_run
             ORDER BY started_at DESC
             LIMIT 40
            """
        )
        entity_status = await conn.fetch(
            """
            SELECT jurisdiction, stage, status, COUNT(*)::bigint AS entities,
                   COALESCE(SUM(rows_in), 0)::bigint AS rows_in,
                   COALESCE(SUM(rows_out), 0)::bigint AS rows_out,
                   MAX(updated_at) AS updated_at
              FROM sec.pipeline_entity_state
             GROUP BY jurisdiction, stage, status
             ORDER BY jurisdiction, stage, status
            """
        )
        filing_state = await conn.fetch(
            """
            SELECT jurisdiction, parsed, COUNT(*)::bigint AS filings,
                   MAX(filed_date) AS max_filed_date, MAX(updated_at) AS updated_at
              FROM sec.source_filing_state
             GROUP BY jurisdiction, parsed
             ORDER BY jurisdiction, parsed
            """
        )
        xbrl_state = await conn.fetch(
            """
            SELECT xbrl_acquisition_status AS status, COUNT(*)::bigint AS filings,
                   MAX(updated_at) AS updated_at
              FROM sec.source_filing_state
             WHERE jurisdiction='US'
             GROUP BY xbrl_acquisition_status
             ORDER BY xbrl_acquisition_status
            """
        )
        market_state = await conn.fetch(
            """
            SELECT source, status, COUNT(*)::bigint AS items,
                   COALESCE(SUM(rows_out), 0)::bigint AS rows_out,
                   MAX(updated_at) AS updated_at
              FROM sec.market_source_item_state
             GROUP BY source, status
             ORDER BY source, status
            """
        )
        scope_state = await conn.fetch(
            """
            SELECT 'US' AS jurisdiction, include_in_pipeline, pipeline_sample_group,
                   COUNT(*)::bigint AS entities
              FROM sec.dim_company_us
             GROUP BY include_in_pipeline, pipeline_sample_group
            UNION ALL
            SELECT 'JP' AS jurisdiction, include_in_pipeline, pipeline_sample_group,
                   COUNT(*)::bigint AS entities
              FROM sec.dim_company_jp
             GROUP BY include_in_pipeline, pipeline_sample_group
             ORDER BY jurisdiction, include_in_pipeline DESC, pipeline_sample_group
            """
        )
        counts = await _fact_counts(conn) if include_counts else []
    return {
        "app_runs": [_record(row) for row in app_rows],
        "stage_runs": [_record(row) for row in stage_rows],
        "entity_status": [_record(row) for row in entity_status],
        "filing_state": [_record(row) for row in filing_state],
        "xbrl_state": [_record(row) for row in xbrl_state],
        "market_state": [_record(row) for row in market_state],
        "scope_state": [_record(row) for row in scope_state],
        "fact_counts": counts,
    }


def _source_filing_where(request: StagingRequest) -> tuple[str, list[Any]]:
    if not request.jurisdiction:
        raise HTTPException(status_code=400, detail="jurisdiction is required")
    args: list[Any] = [request.jurisdiction]
    clauses = ["jurisdiction = $1"]
    entity_ids = _clean_text_list(request.entity_ids)
    if entity_ids:
        args.append(entity_ids)
        clauses.append(f"entity_id = ANY(${len(args)}::text[])")
    if request.source_kind:
        args.append(request.source_kind)
        clauses.append(f"source_kind = ${len(args)}")
    if request.filed_date_from:
        args.append(request.filed_date_from)
        clauses.append(f"filed_date >= ${len(args)}")
    if request.filed_date_to:
        args.append(request.filed_date_to)
        clauses.append(f"filed_date <= ${len(args)}")
    return " AND ".join(clauses), args


async def _staging_source_parsed(conn, request: StagingRequest, apply: bool) -> dict[str, Any]:
    where_sql, args = _source_filing_where(request)
    count = await conn.fetchval(f"SELECT COUNT(*)::bigint FROM sec.source_filing_state WHERE {where_sql}", *args)
    if not apply:
        return {"operation": request.operation, "matching_rows": count, "applied": False}
    tag = await conn.execute(
        f"""
        UPDATE sec.source_filing_state
           SET parsed=FALSE, parse_error=NULL, updated_at=now()
         WHERE {where_sql}
        """,
        *args,
    )
    return {"operation": request.operation, "matching_rows": count, "affected_rows": _affected_count(tag), "applied": True}


async def _staging_us_xbrl(conn, request: StagingRequest, apply: bool) -> dict[str, Any]:
    args: list[Any] = []
    clauses = ["jurisdiction = 'US'"]
    statuses = _clean_text_list(request.statuses)
    entity_ids = _clean_text_list(request.entity_ids)
    if statuses:
        args.append(statuses)
        clauses.append(f"xbrl_acquisition_status = ANY(${len(args)}::text[])")
    if entity_ids:
        args.append(entity_ids)
        clauses.append(f"entity_id = ANY(${len(args)}::text[])")
    where_sql = " AND ".join(clauses)
    count = await conn.fetchval(f"SELECT COUNT(*)::bigint FROM sec.source_filing_state WHERE {where_sql}", *args)
    if not apply:
        return {"operation": request.operation, "matching_rows": count, "applied": False}
    target_status = request.target_status.strip() if request.target_status else "pending"
    if not target_status.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="target_status must be alphanumeric/underscore text")
    args.append(target_status)
    target_placeholder = f"${len(args)}"
    tag = await conn.execute(
        f"""
        UPDATE sec.source_filing_state
           SET xbrl_acquisition_status={target_placeholder},
               xbrl_error=NULL,
               xbrl_download_attempted=FALSE,
               xbrl_last_attempted_at=NULL,
               updated_at=now()
         WHERE {where_sql}
        """,
        *args,
    )
    return {
        "operation": request.operation,
        "matching_rows": count,
        "affected_rows": _affected_count(tag),
        "target_status": target_status,
        "applied": True,
    }


async def _staging_market_item(conn, request: StagingRequest, apply: bool) -> dict[str, Any]:
    if not request.source:
        raise HTTPException(status_code=400, detail="source is required")
    args: list[Any] = [request.source]
    clauses = ["source = $1"]
    source_keys = _clean_text_list(request.source_keys)
    if source_keys:
        args.append(source_keys)
        clauses.append(f"source_key = ANY(${len(args)}::text[])")
    where_sql = " AND ".join(clauses)
    count = await conn.fetchval(f"SELECT COUNT(*)::bigint FROM sec.market_source_item_state WHERE {where_sql}", *args)
    if not apply:
        return {"operation": request.operation, "matching_rows": count, "applied": False}
    args.append(request.clear_hash)
    clear_hash_placeholder = f"${len(args)}"
    tag = await conn.execute(
        f"""
        UPDATE sec.market_source_item_state
           SET status='pending',
               run_id=NULL,
               rows_in=0,
               rows_out=0,
               error_message=NULL,
               started_at=NULL,
               finished_at=NULL,
               source_hash=CASE WHEN {clear_hash_placeholder} THEN NULL ELSE source_hash END,
               updated_at=now()
         WHERE {where_sql}
        """,
        *args,
    )
    return {"operation": request.operation, "matching_rows": count, "affected_rows": _affected_count(tag), "applied": True}


async def _staging_pipeline_scope(conn, request: StagingRequest, apply: bool) -> dict[str, Any]:
    if not request.jurisdiction:
        raise HTTPException(status_code=400, detail="jurisdiction is required")
    entity_ids = _clean_text_list(request.entity_ids)
    if not entity_ids:
        raise HTTPException(status_code=400, detail="entity_ids are required for pipeline_scope_set")
    table = "sec.dim_company_us" if request.jurisdiction == "US" else "sec.dim_company_jp"
    id_col = "cik" if request.jurisdiction == "US" else "edinet_code"
    count = await conn.fetchval(f"SELECT COUNT(*)::bigint FROM {table} WHERE {id_col} = ANY($1::text[])", entity_ids)
    if not apply:
        return {"operation": request.operation, "matching_rows": count, "applied": False}
    include = True if request.include_in_pipeline is None else request.include_in_pipeline
    tag = await conn.execute(
        f"""
        UPDATE {table}
           SET include_in_pipeline=$2,
               pipeline_sample_group=COALESCE($3, pipeline_sample_group),
               updated_at=now()
         WHERE {id_col} = ANY($1::text[])
        """,
        entity_ids,
        include,
        request.sample_group,
    )
    return {
        "operation": request.operation,
        "matching_rows": count,
        "affected_rows": _affected_count(tag),
        "include_in_pipeline": include,
        "applied": True,
    }


async def _run_staging_request(request: StagingRequest, apply: bool) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        if request.operation == "source_parsed_reset":
            return await _staging_source_parsed(conn, request, apply)
        if request.operation == "us_xbrl_status_reset":
            return await _staging_us_xbrl(conn, request, apply)
        if request.operation == "market_item_reset":
            return await _staging_market_item(conn, request, apply)
        if request.operation == "pipeline_scope_set":
            return await _staging_pipeline_scope(conn, request, apply)
    raise HTTPException(status_code=400, detail=f"Unsupported staging operation: {request.operation}")


@router.post("/staging/preview")
async def staging_preview(request: StagingRequest) -> dict[str, Any]:
    return await _run_staging_request(request, apply=False)


@router.post("/staging/apply")
async def staging_apply(request: StagingRequest) -> dict[str, Any]:
    return await _run_staging_request(request, apply=True)


@router.get("/scope-profiles")
async def scope_profiles(jurisdiction: Literal["US", "JP"] | None = Query(default=None)) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        if jurisdiction:
            rows = await conn.fetch(
                """
                SELECT * FROM sec.pipeline_scope_profile
                 WHERE jurisdiction=$1
                 ORDER BY name
                """,
                jurisdiction,
            )
        else:
            rows = await conn.fetch("SELECT * FROM sec.pipeline_scope_profile ORDER BY jurisdiction, name")
    return {"profiles": [_record(row) for row in rows]}


@router.post("/scope-profiles")
async def upsert_scope_profile(request: ScopeProfileRequest) -> dict[str, Any]:
    entity_ids = _clean_text_list(request.entity_ids)
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        row = await conn.fetchrow(
            """
            INSERT INTO sec.pipeline_scope_profile
                (profile_id, jurisdiction, name, description, entity_ids, filters, sample_group)
            VALUES ($1::uuid, $2, $3, $4, $5::text[], $6::jsonb, $7)
            ON CONFLICT (jurisdiction, name) DO UPDATE SET
                description=EXCLUDED.description,
                entity_ids=EXCLUDED.entity_ids,
                filters=EXCLUDED.filters,
                sample_group=EXCLUDED.sample_group,
                updated_at=now()
            RETURNING *
            """,
            str(uuid4()),
            request.jurisdiction,
            request.name.strip(),
            request.description,
            entity_ids,
            json.dumps(request.filters, default=str),
            request.sample_group,
        )
    return {"profile": _record(row)}


@router.post("/scope-profiles/{profile_id}/apply")
async def apply_scope_profile(profile_id: str, request: ScopeProfileApplyRequest) -> dict[str, Any]:
    async with acquire() as conn:
        await _ensure_orchestration_schema(conn)
        profile = await conn.fetchrow("SELECT * FROM sec.pipeline_scope_profile WHERE profile_id=$1::uuid", profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Scope profile not found")
        entity_ids = _clean_text_list(profile["entity_ids"])
        if not entity_ids:
            raise HTTPException(status_code=400, detail="Scope profile has no entity_ids to apply")
        table = "sec.dim_company_us" if profile["jurisdiction"] == "US" else "sec.dim_company_jp"
        id_col = "cik" if profile["jurisdiction"] == "US" else "edinet_code"
        sample_group = request.sample_group or profile["sample_group"]
        async with conn.transaction():
            if request.deactivate_other_entities:
                await conn.execute(f"UPDATE {table} SET include_in_pipeline=FALSE, updated_at=now() WHERE NOT ({id_col} = ANY($1::text[]))", entity_ids)
            tag = await conn.execute(
                f"""
                UPDATE {table}
                   SET include_in_pipeline=$2,
                       pipeline_sample_group=COALESCE($3, pipeline_sample_group),
                       updated_at=now()
                 WHERE {id_col} = ANY($1::text[])
                """,
                entity_ids,
                request.include_in_pipeline,
                sample_group,
            )
    return {
        "profile_id": profile_id,
        "jurisdiction": profile["jurisdiction"],
        "affected_rows": _affected_count(tag),
        "include_in_pipeline": request.include_in_pipeline,
        "sample_group": sample_group,
    }
