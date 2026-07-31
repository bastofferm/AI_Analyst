"""EDINET Daily Acquisition + Refinement Graph (Phase 4).

LangGraph-Implementierung der bestehenden JP-Inkremental-Sequenz plus die
agentischen Veredelungsknoten für JP-GICS und Cross-Source-Drift:

    edinet_master_sync → filing_index_diff → download_zip_dispatch (Send)
    → download_zip_one → publicdoc_extract → raw_parse
    → gics_enrich (mit LLM-Fallback) → isin_enrich → standardize
    → metrics_compute → recon → cross_source_recon → drift_explain
    → quality_gate → [human_review bei human_review] → publish

Die Knoten kapseln die existierenden Funktionen aus
`xbrl_sec.sec.pipelines.jp`. Agentische Knoten leben in:
- xbrl_sec.sec.graphs.gics_enrich   (LLM-Fallback für JP-Industry → GICS)
- xbrl_sec.sec.graphs.drift_explain (SEC vs EDINET Drift-Klassifizierung)
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Command, Send, interrupt

from xbrl_sec.llm import setup_llm_cache
from xbrl_sec.llm.approvals import create_pending_approval, record_decision
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.graphs._bucket import (
    DOWNLOAD_BUCKET_SIZE,
    bucket_id,
    chunk_ids,
    log_bucket_finish,
    log_bucket_start,
    merge_bucket_ledger,
)
from xbrl_sec.sec.graphs.drift_explain import detect_cross_source_drift, explain_drift_batch
from xbrl_sec.sec.graphs.gics_enrich import enrich_unmapped_gics


GRAPH_NAME = "edinet_daily"
_DOWNLOAD_BUCKET_SIZE = DOWNLOAD_BUCKET_SIZE
_BUCKET_STAGE = "jp_download_bucket"


class EDINETDailyState(TypedDict, total=False):
    thread_id: str
    run_date: str
    jurisdiction: Literal["JP"]
    only_codes: list[str]
    code_limit: int | None
    skip_download: bool
    skip_drift: bool
    force_rerun: bool

    master_sync_summary: dict[str, Any]
    filing_index_diff_summary: dict[str, Any]
    download_summary: dict[str, Any]
    download_pending_doc_ids: list[str]
    bucket_ledger: Annotated[dict[str, dict[str, Any]], merge_bucket_ledger]
    publicdoc_summary: dict[str, Any]
    raw_parse_summary: dict[str, Any]
    gics_summary: dict[str, Any]
    isin_summary: dict[str, Any]
    standardize_summary: dict[str, Any]
    metrics_summary: dict[str, Any]
    recon_summary: dict[str, Any]
    cross_source_summary: dict[str, Any]
    drift_summary: dict[str, Any]
    quality_status: Literal["green", "yellow", "red"]
    approval_request: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    errors: list[dict[str, str]]
    published_at: str | None


def _record_error(state: EDINETDailyState, stage: str, exc: Exception) -> None:
    errors = list(state.get("errors") or [])
    errors.append({"stage": stage, "type": exc.__class__.__name__, "message": str(exc)[:300]})
    state["errors"] = errors


def edinet_master_sync_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.jp import refresh_jp_master

    try:
        rows = refresh_jp_master(full=False)
        return {"master_sync_summary": {"rows": int(rows or 0)}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "edinet_master_sync", exc)
        return {"master_sync_summary": {"error": str(exc)[:300]}}


def filing_index_diff_node(state: EDINETDailyState) -> dict[str, Any]:
    sql = """
        SELECT COUNT(*) FROM source_filing_state
        WHERE jurisdiction = 'JP'
          AND (parsed IS NULL OR parsed = FALSE)
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            pending = int(cur.fetchone()[0] or 0)
        return {"filing_index_diff_summary": {"pending_filings": pending}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "filing_index_diff", exc)
        return {"filing_index_diff_summary": {"error": str(exc)[:300]}}


def _fetch_pending_doc_ids(limit: int | None) -> list[str]:
    """Concrete doc_ids still needing download or re-download.

    Delegates to the source-layer helper so we stay in sync with what
    ``download_jp_xbrl`` would fetch when called without an explicit doc_ids list.
    """
    from xbrl_sec.sec.sources.edinet_download import _pending_doc_ids as pending_pairs

    pairs = pending_pairs(force=False, limit=limit)
    return [doc_id for doc_id, _redownload in pairs]


def download_zip_dispatch_node(state: EDINETDailyState) -> dict[str, Any]:
    """Materialize the pending doc_id list; fan-out hands each bucket its own IDs."""
    if state.get("skip_download"):
        return {"download_summary": {"skipped": True}, "download_pending_doc_ids": []}
    try:
        doc_ids = _fetch_pending_doc_ids(state.get("code_limit"))
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "download_zip_dispatch", exc)
        return {
            "download_summary": {"scope": 0, "buckets": 0, "error": str(exc)[:300]},
            "download_pending_doc_ids": [],
        }
    buckets = chunk_ids(doc_ids)
    return {
        "download_pending_doc_ids": [d for chunk in buckets for d in chunk],
        "download_summary": {"scope": sum(len(b) for b in buckets), "buckets": len(buckets)},
    }


def _fanout_download_buckets(state: EDINETDailyState) -> list[Send]:
    summary = state.get("download_summary") or {}
    if summary.get("skipped"):
        return [Send("publicdoc_extract", dict(state))]
    pending = state.get("download_pending_doc_ids") or []
    if not pending:
        return [Send("publicdoc_extract", dict(state))]

    ledger = state.get("bucket_ledger") or {}
    force = bool(state.get("force_rerun"))
    buckets = chunk_ids(pending)
    sends: list[Send] = []
    for idx, bucket in enumerate(buckets):
        bid = bucket_id(GRAPH_NAME, state["thread_id"], idx)
        prev = ledger.get(bid) or {}
        if not force and prev.get("status") == "succeeded":
            continue
        sends.append(
            Send(
                "download_zip_one",
                {
                    "thread_id": state["thread_id"],
                    "doc_ids": bucket,
                    "bucket_id": bid,
                    "bucket_index": idx,
                },
            )
        )
    return sends or [Send("publicdoc_extract", dict(state))]


def download_zip_one_node(state: dict[str, Any]) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.jp import download_jp_xbrl

    doc_ids = list(state.get("doc_ids") or [])
    bid = state.get("bucket_id") or bucket_id(GRAPH_NAME, state.get("thread_id", ""), int(state.get("bucket_index") or 0))
    bucket_index = int(state.get("bucket_index") or 0)
    thread_id = state.get("thread_id", "")

    ctx = log_bucket_start(GRAPH_NAME, thread_id, _BUCKET_STAGE, bucket_index, doc_ids, "JP")
    try:
        result = download_jp_xbrl(doc_ids=doc_ids, force=False)
        rows_out = 0
        if isinstance(result, tuple) and result:
            rows_out = int(result[0] or 0)
        log_bucket_finish(ctx, "succeeded", rows_in=len(doc_ids), rows_out=rows_out)
        return {
            "bucket_ledger": {
                bid: {
                    "status": "succeeded",
                    "bucket_index": bucket_index,
                    "bucket_size": len(doc_ids),
                    "rows_in": len(doc_ids),
                    "rows_out": rows_out,
                    "first_id": doc_ids[0] if doc_ids else None,
                    "last_id": doc_ids[-1] if doc_ids else None,
                }
            }
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        log_bucket_finish(ctx, "failed", rows_in=len(doc_ids), rows_out=0, error=message)
        return {
            "bucket_ledger": {
                bid: {
                    "status": "failed",
                    "bucket_index": bucket_index,
                    "bucket_size": len(doc_ids),
                    "rows_in": len(doc_ids),
                    "rows_out": 0,
                    "first_id": doc_ids[0] if doc_ids else None,
                    "last_id": doc_ids[-1] if doc_ids else None,
                    "error": message,
                }
            }
        }


def publicdoc_extract_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.jp import extract_jp_xbrl

    only = state.get("only_codes") or None
    try:
        extract_jp_xbrl(entity_ids=only, force=False)
        return {"publicdoc_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "publicdoc_extract", exc)
        return {"publicdoc_summary": {"error": str(exc)[:300]}}


def raw_parse_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.jp import parse_jp_raw

    only = state.get("only_codes") or None
    try:
        rows = parse_jp_raw(entity_ids=only, force=False)
        return {"raw_parse_summary": {"rows": int(rows or 0)}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "raw_parse", exc)
        return {"raw_parse_summary": {"error": str(exc)[:300]}}


def gics_enrich_node(state: EDINETDailyState) -> dict[str, Any]:
    """First run the deterministic TSE33→GICS pass, then call the LLM agent
    for whatever remains unmapped."""
    from xbrl_sec.sec.pipelines.jp import enrich_jp_gics

    try:
        enrich_jp_gics(full=False)
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "gics_csv_enrich", exc)
    try:
        agent_summary = enrich_unmapped_gics(limit=25)
        return {"gics_summary": agent_summary}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "gics_llm_enrich", exc)
        return {"gics_summary": {"error": str(exc)[:300]}}


def isin_enrich_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.jp import enrich_jp_isin

    try:
        enrich_jp_isin(full=False)
        return {"isin_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "isin_enrich", exc)
        return {"isin_summary": {"error": str(exc)[:300]}}


def standardize_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.std.jp_standardize import populate_jp_std

    try:
        populate_jp_std(full=False)
        return {"standardize_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "standardize", exc)
        return {"standardize_summary": {"error": str(exc)[:300]}}


def metrics_compute_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.metrics.compute import compute_metrics

    try:
        compute_metrics("JP", full=False)
        return {"metrics_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "metrics_compute", exc)
        return {"metrics_summary": {"error": str(exc)[:300]}}


def recon_node(state: EDINETDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.metrics.recon import build_recon

    try:
        build_recon("JP", full=False)
        return {"recon_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "recon", exc)
        return {"recon_summary": {"error": str(exc)[:300]}}


def cross_source_recon_node(state: EDINETDailyState) -> dict[str, Any]:
    if state.get("skip_drift"):
        return {"cross_source_summary": {"skipped": True}}
    try:
        rows = detect_cross_source_drift(threshold_pct=0.05, limit=25)
        return {
            "cross_source_summary": {
                "drift_rows": len(rows),
                "rows": rows[:5],
                "total": len(rows),
            }
        }
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "cross_source_recon", exc)
        return {"cross_source_summary": {"error": str(exc)[:300]}}


def drift_explain_node(state: EDINETDailyState) -> dict[str, Any]:
    if state.get("skip_drift"):
        return {"drift_summary": {"skipped": True}}
    summary = state.get("cross_source_summary") or {}
    if summary.get("skipped") or summary.get("error"):
        return {"drift_summary": {"skipped": True}}
    # Re-derive the rows from the previous step so we don't carry bulk data in
    # the persisted state. Caps at 25.
    try:
        rows = detect_cross_source_drift(threshold_pct=0.05, limit=25)
        if not rows:
            return {"drift_summary": {"rows": 0}}
        result = explain_drift_batch(rows)
        return {"drift_summary": result}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "drift_explain", exc)
        return {"drift_summary": {"error": str(exc)[:300]}}


def quality_gate_node(state: EDINETDailyState) -> dict[str, Any]:
    errors = state.get("errors") or []
    drift = state.get("drift_summary") or {}
    halt = int(drift.get("halt_pipeline") or 0)
    review = int(drift.get("human_review") or 0)

    if errors or halt:
        status = "red"
    elif review > 0:
        status = "yellow"
    else:
        status = "green"

    approval_request: dict[str, Any] | None = None
    if status != "green":
        request = create_pending_approval(
            thread_id=state["thread_id"],
            graph_name=GRAPH_NAME,
            node_name="quality_gate",
            payload={
                "status": status,
                "errors": errors,
                "drift_summary": drift,
                "gics_summary": state.get("gics_summary"),
                "cross_source_summary": state.get("cross_source_summary"),
            },
        )
        approval_request = {
            "approval_id": str(request.approval_id),
            "node": request.node_name,
        }
    return {"quality_status": status, "approval_request": approval_request}


def _route_after_quality_gate(state: EDINETDailyState) -> Literal["human_review", "publish"]:
    return "human_review" if state.get("quality_status") != "green" else "publish"


def human_review_node(state: EDINETDailyState) -> dict[str, Any]:
    request = state.get("approval_request") or {}
    decision = interrupt(
        {
            "graph": GRAPH_NAME,
            "approval_id": request.get("approval_id"),
            "thread_id": state["thread_id"],
            "summary": {
                "status": state.get("quality_status"),
                "drift_human_review": (state.get("drift_summary") or {}).get("human_review", 0),
                "gics_queued": (state.get("gics_summary") or {}).get("queued_for_review", 0),
                "errors": len(state.get("errors") or []),
            },
        }
    )
    if isinstance(decision, dict) and request.get("approval_id"):
        try:
            record_decision(
                approval_id=request["approval_id"],
                decision=decision,
                decided_by=decision.get("decided_by"),
                status="approved" if decision.get("approve") else "rejected",
            )
        except Exception:
            pass
    return {"approval_decision": decision if isinstance(decision, dict) else {"approve": True}}


def publish_node(state: EDINETDailyState) -> dict[str, Any]:
    return {"published_at": datetime.now(timezone.utc).isoformat()}


def build_graph() -> Any:
    g = StateGraph(EDINETDailyState)
    g.add_node("edinet_master_sync", edinet_master_sync_node)
    g.add_node("filing_index_diff", filing_index_diff_node)
    g.add_node("download_zip_dispatch", download_zip_dispatch_node)
    g.add_node("download_zip_one", download_zip_one_node)
    g.add_node("publicdoc_extract", publicdoc_extract_node)
    g.add_node("raw_parse", raw_parse_node)
    g.add_node("gics_enrich", gics_enrich_node)
    g.add_node("isin_enrich", isin_enrich_node)
    g.add_node("standardize", standardize_node)
    g.add_node("metrics_compute", metrics_compute_node)
    g.add_node("recon", recon_node)
    g.add_node("cross_source_recon", cross_source_recon_node)
    g.add_node("drift_explain", drift_explain_node)
    g.add_node("quality_gate", quality_gate_node)
    g.add_node("human_review", human_review_node)
    g.add_node("publish", publish_node)

    g.add_edge(START, "edinet_master_sync")
    g.add_edge("edinet_master_sync", "filing_index_diff")
    g.add_edge("filing_index_diff", "download_zip_dispatch")
    g.add_conditional_edges(
        "download_zip_dispatch",
        _fanout_download_buckets,
        ["download_zip_one", "publicdoc_extract"],
    )
    g.add_edge("download_zip_one", "publicdoc_extract")
    g.add_edge("publicdoc_extract", "raw_parse")
    g.add_edge("raw_parse", "gics_enrich")
    g.add_edge("gics_enrich", "isin_enrich")
    g.add_edge("isin_enrich", "standardize")
    g.add_edge("standardize", "metrics_compute")
    g.add_edge("metrics_compute", "recon")
    g.add_edge("recon", "cross_source_recon")
    g.add_edge("cross_source_recon", "drift_explain")
    g.add_edge("drift_explain", "quality_gate")
    g.add_conditional_edges(
        "quality_gate",
        _route_after_quality_gate,
        {"human_review": "human_review", "publish": "publish"},
    )
    g.add_edge("human_review", "publish")
    g.add_edge("publish", END)
    return g


def compile_edinet_daily_graph(checkpointer: Any | None = None) -> Any:
    graph = build_graph()
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def _default_initial_state(args: argparse.Namespace) -> EDINETDailyState:
    return EDINETDailyState(
        thread_id=args.thread_id,
        run_date=date.today().isoformat(),
        jurisdiction="JP",
        only_codes=args.edinet_code or [],
        code_limit=args.limit,
        skip_download=args.skip_download,
        skip_drift=args.skip_drift,
        force_rerun=bool(getattr(args, "force_rerun", False)),
        errors=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the EDINET daily LangGraph pipeline")
    parser.add_argument("--thread-id", default=f"edinet-daily-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--edinet-code", action="append", default=[], help="Restrict to these EDINET codes (repeatable).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-drift", action="store_true")
    parser.add_argument("--force-rerun", action="store_true",
                        help="Ignore bucket_ledger — re-fire every download bucket even if a prior run succeeded.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_llm_cache()
    os.environ.setdefault("MZQA_PIPELINE_APP_RUN_ID", uuid.uuid4().hex)

    compiled = compile_edinet_daily_graph()
    initial = _default_initial_state(args)

    if args.dry_run:
        dispatch_state = dict(initial)
        dispatch_state.update(download_zip_dispatch_node(dispatch_state))
        pending = list(dispatch_state.get("download_pending_doc_ids") or [])
        buckets = chunk_ids(pending)
        plan = [
            {
                "bucket_index": i,
                "bucket_id": bucket_id(GRAPH_NAME, args.thread_id, i),
                "bucket_size": len(chunk),
                "first_id": chunk[0] if chunk else None,
                "last_id": chunk[-1] if chunk else None,
                "ids_head": chunk[:3],
            }
            for i, chunk in enumerate(buckets)
        ]
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "thread_id": args.thread_id,
                    "nodes": sorted(compiled.get_graph().nodes),
                    "download_summary": dispatch_state.get("download_summary"),
                    "bucket_plan": plan,
                },
                ensure_ascii=False,
            )
        )
        return 0

    config = {"configurable": {"thread_id": args.thread_id}}
    final_state: dict[str, Any] = {}
    for event in compiled.stream(initial, config=config, stream_mode="values"):
        final_state = event
    if "__interrupt__" in final_state:
        print(
            json.dumps(
                {
                    "interrupted": True,
                    "thread_id": args.thread_id,
                    "approval_request": final_state.get("approval_request"),
                    "quality_status": final_state.get("quality_status"),
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "thread_id": args.thread_id,
                "quality_status": final_state.get("quality_status"),
                "errors": final_state.get("errors"),
                "raw_parse_summary": final_state.get("raw_parse_summary"),
                "gics_summary": final_state.get("gics_summary"),
                "drift_summary": final_state.get("drift_summary"),
                "published_at": final_state.get("published_at"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def resume_graph(thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    compiled = compile_edinet_daily_graph()
    config = {"configurable": {"thread_id": thread_id}}
    final_state: dict[str, Any] = {}
    for event in compiled.stream(Command(resume=decision), config=config, stream_mode="values"):
        final_state = event
    return final_state


if __name__ == "__main__":
    raise SystemExit(main())
