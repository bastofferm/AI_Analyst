"""Background job: run the research loop off the request thread and persist it as it goes.

The one-shot ``POST /api/quant/retrain`` holds an HTTP request open for the 1-3 minutes a
single fit takes. A research run is several fits plus a dozen LLM calls — comfortably past
any proxy or browser timeout — so it cannot use that shape. Instead it follows the pattern
already established by ``fact_cycle_ic_job_status``: a durable ledger row the client polls,
with the work on a daemon thread.

Three properties this buys, all of which the blocking shape cannot provide:

* A run survives the page being closed and reloaded. The UI re-attaches by ``run_id``.
* Rounds are persisted as they complete. A run that dies in round 4 keeps rounds 1-3, and
  their reports remain readable.
* Cancellation is cooperative and checked between rounds, so a run stopped by the user
  leaves a consistent ledger rather than a half-written row.

Concurrency is deliberately narrow: one active run per ``(model_key, label)``, enforced both
in-process and by a DB check, because two runs training the same model would race on the
artifact path at promotion time.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("mzqa.quant.research.runner")

# In-process registry of live runs: run_id -> {"cancel": Event, "thread": Thread, "key": str}
_ACTIVE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

TERMINAL = ("complete", "failed", "cancelled")


# --------------------------------------------------------------------------- #
# Schema (idempotent — mirrors qlib_train_all._ensure_tables)
# --------------------------------------------------------------------------- #
def _ensure_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_alpha_research_run (
            run_id TEXT PRIMARY KEY, model_key TEXT NOT NULL, jurisdiction TEXT NOT NULL,
            label TEXT NOT NULL, status TEXT NOT NULL, provider TEXT, advisor_provider TEXT,
            max_iterations INTEGER NOT NULL DEFAULT 4, iterations_done INTEGER NOT NULL DEFAULT 0,
            current_stage TEXT, baseline_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            champion_iteration INTEGER, champion_kind TEXT, champion_score DOUBLE PRECISION,
            promoted BOOLEAN NOT NULL DEFAULT FALSE, promotion_reason TEXT, stop_reason TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ, elapsed_seconds DOUBLE PRECISION, error TEXT,
            summary_json JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_alpha_research_iteration (
            run_id TEXT NOT NULL, iteration INTEGER NOT NULL,
            spec_json JSONB NOT NULL DEFAULT '{}'::jsonb, spec_hash TEXT,
            patch_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            rating_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            breakdown_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            validation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            pm_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            advisor_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            researcher_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            artifact_path TEXT, elapsed_seconds DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, iteration)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS quant_alpha_research_run_lookup_idx "
                "ON quant_alpha_research_run (model_key, label, started_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS quant_alpha_research_iteration_run_idx "
                "ON quant_alpha_research_iteration (run_id)")


def _connect():
    from xbrl_sec.sec.db.connection import connect

    return connect()


def _json(value: Any) -> str:
    """Serialize for JSONB, coercing NaN/inf to null (both are invalid JSON)."""
    import math

    def _clean(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_clean(x) for x in v]
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v

    return json.dumps(_clean(value), default=str)


# --------------------------------------------------------------------------- #
# Ledger writes
# --------------------------------------------------------------------------- #
def _insert_run(run_id: str, *, model_key: str, jurisdiction: str, label: str,
                provider: str | None, advisor_provider: str | None,
                max_iterations: int) -> None:
    with _connect() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute(
            """
            INSERT INTO quant_alpha_research_run
                (run_id, model_key, jurisdiction, label, status, provider, advisor_provider,
                 max_iterations, current_stage)
            VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,'queued')
            """,
            (run_id, model_key, jurisdiction, label, provider, advisor_provider,
             int(max_iterations)),
        )
        conn.commit()


def _update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE quant_alpha_research_run SET {cols}, updated_at = now() "
                f"WHERE run_id = %s",
                (*fields.values(), run_id),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 - a status write must never kill the run it describes
        logger.warning("run status update failed for %s", run_id, exc_info=True)


def _write_iteration(run_id: str, report: dict[str, Any],
                     agent_notes: list[dict[str, Any]] | None = None) -> None:
    """Persist one completed round. Called from the graph's on_iteration hook."""
    notes = {n.get("role"): n for n in (agent_notes or [])
             if n.get("iteration") == report.get("iteration")}
    sections = report.get("sections", {})
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            cur.execute(
                """
                INSERT INTO quant_alpha_research_iteration
                    (run_id, iteration, spec_json, spec_hash, patch_json, metrics_json,
                     rating_json, breakdown_json, validation_json, pm_json, advisor_json,
                     researcher_json, report_json, elapsed_seconds)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, iteration) DO UPDATE SET
                    spec_json=EXCLUDED.spec_json, spec_hash=EXCLUDED.spec_hash,
                    patch_json=EXCLUDED.patch_json, metrics_json=EXCLUDED.metrics_json,
                    rating_json=EXCLUDED.rating_json, breakdown_json=EXCLUDED.breakdown_json,
                    validation_json=EXCLUDED.validation_json, pm_json=EXCLUDED.pm_json,
                    advisor_json=EXCLUDED.advisor_json,
                    researcher_json=EXCLUDED.researcher_json,
                    report_json=EXCLUDED.report_json,
                    elapsed_seconds=EXCLUDED.elapsed_seconds
                """,
                (run_id, int(report.get("iteration", 0)),
                 _json(report.get("spec", {})), report.get("spec_hash"),
                 _json({"changes": report.get("spec_changes", []),
                        "rejected": report.get("spec_rejected", [])}),
                 _json({k: v for k, v in sections.items()
                        if k in ("functional_correctness", "ranking_quality",
                                 "economic_value", "robustness", "explainability",
                                 "factor_hygiene", "consistency", "monitorability")}),
                 _json((sections.get("robustness", {}) or {}).get("perturbation_rating", {})),
                 _json(sections.get("domain_adaptability", {})),
                 _json((notes.get("validation") or {}).get("verdict", {})),
                 _json((notes.get("portfolio_manager") or {}).get("verdict", {})),
                 _json((notes.get("external_advisor") or {}).get("note", {})),
                 _json((notes.get("researcher") or {}).get("proposal", {})),
                 _json(report), report.get("elapsed_seconds")),
            )
            cur.execute(
                "UPDATE quant_alpha_research_run SET iterations_done = %s, updated_at = now() "
                "WHERE run_id = %s", (int(report.get("iteration", 0)), run_id))
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("iteration persistence failed for %s round %s",
                       run_id, report.get("iteration"), exc_info=True)


# --------------------------------------------------------------------------- #
# Starting and driving a run
# --------------------------------------------------------------------------- #
def model_key_for(jurisdiction: str) -> str:
    return (jurisdiction or "US").upper()


def active_run_for(model_key: str, label: str) -> str | None:
    """The run_id of a live run for this model, if any (in-process, then the ledger)."""
    with _LOCK:
        for run_id, entry in _ACTIVE.items():
            if entry["key"] == f"{model_key}|{label}" and entry["thread"].is_alive():
                return run_id
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            cur.execute(
                "SELECT run_id FROM quant_alpha_research_run "
                "WHERE model_key = %s AND label = %s AND status IN ('queued','running') "
                "ORDER BY started_at DESC LIMIT 1", (model_key, label))
            row = cur.fetchone()
            return str(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


def start_run(
    *,
    jurisdiction: str = "US",
    label: str = "forward_1m",
    max_iterations: int = 4,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    advisor_provider: str | None = None,
    advisor_api_key: str | None = None,
    advisor_model: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register a run and start it on a daemon thread. Returns immediately."""
    model_key = model_key_for(jurisdiction)
    existing = active_run_for(model_key, label)
    if existing:
        return {"ok": False, "run_id": existing,
                "error": f"a research run for {model_key} {label} is already in progress"}

    run_id = uuid.uuid4().hex
    try:
        _insert_run(run_id, model_key=model_key, jurisdiction=jurisdiction, label=label,
                    provider=provider, advisor_provider=advisor_provider,
                    max_iterations=max_iterations)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not register the run: "
                                      f"{type(exc).__name__}: {exc}"}

    cancel = threading.Event()
    thread = threading.Thread(
        target=_run_worker, name=f"alpha-research-{run_id[:8]}", daemon=True,
        kwargs=dict(run_id=run_id, jurisdiction=jurisdiction, label=label,
                    max_iterations=max_iterations, provider=provider, api_key=api_key,
                    model=model, advisor_provider=advisor_provider,
                    advisor_api_key=advisor_api_key, advisor_model=advisor_model,
                    config=config or {}, cancel=cancel),
    )
    with _LOCK:
        _ACTIVE[run_id] = {"cancel": cancel, "thread": thread,
                           "key": f"{model_key}|{label}"}
    thread.start()
    return {"ok": True, "run_id": run_id, "status": "running",
            "model_key": model_key, "label": label}


def _run_worker(*, run_id: str, jurisdiction: str, label: str, max_iterations: int,
                provider: str | None, api_key: str | None, model: str | None,
                advisor_provider: str | None, advisor_api_key: str | None,
                advisor_model: str | None, config: dict[str, Any],
                cancel: threading.Event) -> None:
    from . import graph as graph_mod

    started = time.time()
    _update_run(run_id, status="running", current_stage="starting")
    notes_ref: dict[str, Any] = {"notes": []}

    def _progress(stage: str) -> None:
        _update_run(run_id, current_stage=stage[:200])

    def _on_iteration(report: dict[str, Any]) -> None:
        _write_iteration(run_id, report, notes_ref.get("notes"))

    try:
        final = graph_mod.run_research(
            jurisdiction, label, max_iterations=max_iterations,
            provider=provider, api_key=api_key, model=model,
            advisor_provider=advisor_provider, advisor_api_key=advisor_api_key,
            advisor_model=advisor_model, config=config, run_id=run_id,
            progress=_progress, on_iteration=_on_iteration,
            should_cancel=cancel.is_set,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("research run %s failed", run_id)
        _update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                    completed_at=datetime.now(timezone.utc),
                    elapsed_seconds=round(time.time() - started, 1),
                    current_stage="failed")
        _forget(run_id)
        return

    # Re-persist every round now that the agents' verdicts for it exist: the on_iteration
    # hook fires from report_node, which runs BEFORE the three critics have spoken.
    notes = final.get("agent_notes") or []
    for rep in final.get("iterations") or []:
        _write_iteration(run_id, rep, notes)

    summary = graph_mod.summarize(final)
    champion = summary.get("champion") or {}
    _update_run(
        run_id,
        status="cancelled" if cancel.is_set() else "complete",
        current_stage="done",
        iterations_done=summary.get("iterations_done", 0),
        champion_iteration=champion.get("iteration"),
        champion_kind=champion.get("kind"),
        champion_score=champion.get("score"),
        promoted=bool(summary.get("promoted")),
        promotion_reason=(summary.get("promotion_reason") or "")[:2000],
        stop_reason=(summary.get("stop_reason") or "")[:500],
        baseline_json=_json(summary.get("incumbent") or {}),
        summary_json=_json(summary),
        completed_at=datetime.now(timezone.utc),
        elapsed_seconds=round(time.time() - started, 1),
        error=("; ".join(f"{e.get('stage')}: {e.get('message')}"
                         for e in summary.get("errors") or [])[:2000] or None),
    )
    _forget(run_id)


def _forget(run_id: str) -> None:
    with _LOCK:
        _ACTIVE.pop(run_id, None)


def cancel_run(run_id: str) -> dict[str, Any]:
    """Cooperative cancel — the flag is checked between rounds, not mid-fit."""
    with _LOCK:
        entry = _ACTIVE.get(run_id)
    if not entry:
        return {"ok": False, "error": "no active run with that id "
                                      "(it may have already finished)"}
    entry["cancel"].set()
    _update_run(run_id, current_stage="cancelling")
    return {"ok": True, "run_id": run_id,
            "note": "the run will stop after the current round completes"}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
_RUN_COLUMNS = (
    "run_id, model_key, jurisdiction, label, status, provider, advisor_provider, "
    "max_iterations, iterations_done, current_stage, baseline_json, champion_iteration, "
    "champion_kind, champion_score, promoted, promotion_reason, stop_reason, started_at, "
    "updated_at, completed_at, elapsed_seconds, error, summary_json"
)


def _row_to_dict(cur, row) -> dict[str, Any]:
    cols = [d[0] for d in cur.description]
    out = dict(zip(cols, row))
    for key, value in list(out.items()):
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


def get_run(run_id: str, *, with_iterations: bool = True) -> dict[str, Any] | None:
    """The run header plus, by default, every persisted round. The polling endpoint."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            cur.execute(f"SELECT {_RUN_COLUMNS} FROM quant_alpha_research_run "
                        f"WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            run = _row_to_dict(cur, row)
            if with_iterations:
                # metrics_json and breakdown_json exist so a consumer can read the battery
                # and the sub-population tables WITHOUT unpacking the full report blob;
                # omitting them here made those columns write-only.
                cur.execute(
                    "SELECT iteration, spec_hash, patch_json, metrics_json, rating_json, "
                    "breakdown_json, validation_json, pm_json, advisor_json, "
                    "researcher_json, report_json, elapsed_seconds "
                    "FROM quant_alpha_research_iteration WHERE run_id = %s "
                    "ORDER BY iteration", (run_id,))
                run["iterations"] = [_row_to_dict(cur, r) for r in cur.fetchall()]
            return run
    except Exception:  # noqa: BLE001
        logger.warning("run fetch failed for %s", run_id, exc_info=True)
        return None


def latest_run(jurisdiction: str = "US", label: str = "forward_1m") -> dict[str, Any] | None:
    """Most recent run for this model, so the panel opens populated."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            cur.execute(
                "SELECT run_id FROM quant_alpha_research_run WHERE model_key = %s "
                "AND label = %s ORDER BY started_at DESC LIMIT 1",
                (model_key_for(jurisdiction), label))
            row = cur.fetchone()
            return get_run(str(row[0])) if row else None
    except Exception:  # noqa: BLE001
        return None


def list_runs(jurisdiction: str | None = None, label: str | None = None,
              limit: int = 20) -> list[dict[str, Any]]:
    """Run history for the market/horizon, newest first (no iteration payloads)."""
    filters, params = [], []
    if jurisdiction:
        filters.append("model_key = %s")
        params.append(model_key_for(jurisdiction))
    if label:
        filters.append("label = %s")
        params.append(label)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure_tables(cur)
            cur.execute(
                f"SELECT run_id, model_key, label, status, iterations_done, max_iterations, "
                f"champion_iteration, champion_kind, champion_score, promoted, stop_reason, "
                f"started_at, completed_at, elapsed_seconds "
                f"FROM quant_alpha_research_run {where} ORDER BY started_at DESC LIMIT %s",
                (*params, int(limit)))
            return [_row_to_dict(cur, r) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        logger.warning("run list failed", exc_info=True)
        return []
