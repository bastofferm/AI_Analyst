"""SEC Daily Incremental Graph (Phase 3).

LangGraph-Implementierung der bestehenden run_incremental-Sequenz plus
agentische Veredelungsknoten:

    cik_diff → filing_diff → download_dispatch (Send pro CIK-Bucket)
    → download_one → xbrl_extract → parse_raw
    → auto_map_unknown_concepts (LLM-Agent)
    → standardize → metrics_compute → recon
    → [sec_text_extract Subgraph: optional] → quality_gate
    → [human_review bei Drift] → publish

Die Knoten sind dünne Wrapper um `xbrl_sec.sec.pipelines.us` — keine
Geschäftslogik wird dupliziert. Echter LLM-Code lebt in:
- xbrl_sec.sec.graphs.concept_mapping (Concept-Mapping-Agent)
- xbrl_sec.sec.graphs.text_extract (Filing-Sektionsextraktion)

State enthält nur IDs, Counters, Cursors. Bulk-Daten gehen direkt in die
Fact-Tabellen.
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
from xbrl_sec.sec.graphs.concept_mapping import auto_map_batch, fetch_unmapped_concepts
from xbrl_sec.sec.graphs.text_extract import extract_filing_sections, fetch_recent_unparsed_filings


GRAPH_NAME = "sec_daily"
_DOWNLOAD_BUCKET_SIZE = DOWNLOAD_BUCKET_SIZE
_BUCKET_STAGE = "us_download_bucket"
_MAX_CONCEPT_MAPPING_TARGETS = 25


class SECDailyState(TypedDict, total=False):
    thread_id: str
    run_date: str
    jurisdiction: Literal["US"]
    only_ciks: list[str]
    cik_limit: int | None
    extract_sections: bool
    skip_download: bool
    skip_text_extract: bool
    force_rerun: bool

    cik_diff_count: int
    filing_diff_summary: dict[str, Any]
    download_summary: dict[str, Any]
    download_pending_ciks: list[str]
    bucket_ledger: Annotated[dict[str, dict[str, Any]], merge_bucket_ledger]
    xbrl_extract_summary: dict[str, Any]
    parse_raw_summary: dict[str, Any]
    concept_mapping_summary: dict[str, Any]
    standardize_summary: dict[str, Any]
    metrics_summary: dict[str, Any]
    recon_summary: dict[str, Any]
    text_extract_summary: dict[str, Any]
    quality_status: Literal["green", "yellow", "red"]
    approval_request: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    errors: list[dict[str, str]]
    published_at: str | None


def _record_error(state: SECDailyState, stage: str, exc: Exception) -> None:
    errors = list(state.get("errors") or [])
    errors.append({"stage": stage, "type": exc.__class__.__name__, "message": str(exc)[:300]})
    state["errors"] = errors


def _normalize_ciks(values: list[str]) -> list[str]:
    return [v.zfill(10) for v in values if v and v.strip()]


def cik_diff_node(state: SECDailyState) -> dict[str, Any]:
    """Identify CIKs whose master record changed since the last run.

    The pipeline already tracks this via `pipeline_entity_state`; we count
    rows that haven't been synced today.
    """
    sql = """
        SELECT COUNT(*) FROM dim_company_us c
        WHERE c.include_in_pipeline = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM pipeline_entity_state s
              WHERE s.jurisdiction = 'US'
                AND s.entity_id = c.cik
                AND s.stage = 'master'
                AND s.updated_at >= NOW() - INTERVAL '24 hours'
          )
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            return {"cik_diff_count": int(cur.fetchone()[0] or 0)}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "cik_diff", exc)
        return {"cik_diff_count": 0}


def filing_diff_node(state: SECDailyState) -> dict[str, Any]:
    """Count filings observed by the source-state tracker but not yet parsed."""
    only = _normalize_ciks(state.get("only_ciks") or [])
    where = ["jurisdiction = 'US'", "(parsed IS NULL OR parsed = FALSE)"]
    params: list[Any] = []
    if only:
        where.append("entity_id = ANY(%s)")
        params.append(only)
    sql = f"""
        SELECT COUNT(*),
               COUNT(DISTINCT entity_id),
               MAX(filed_date)
        FROM source_filing_state
        WHERE {' AND '.join(where)}
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params or None)
            row = cur.fetchone()
            return {
                "filing_diff_summary": {
                    "pending_filings": int(row[0] or 0),
                    "pending_entities": int(row[1] or 0),
                    "latest_filed_date": row[2].isoformat() if row[2] else None,
                }
            }
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "filing_diff", exc)
        return {"filing_diff_summary": {"error": str(exc)[:300]}}


def _fetch_pending_ciks(only: list[str] | None) -> list[str]:
    """CIKs with at least one source_filing_state row still needing work."""
    where = [
        "s.jurisdiction = 'US'",
        "(s.downloaded = FALSE OR s.extracted = FALSE OR s.parsed = FALSE)",
    ]
    params: list[Any] = []
    if only:
        where.append("s.entity_id = ANY(%s)")
        params.append(list(only))
    sql = f"""
        SELECT DISTINCT s.entity_id
          FROM source_filing_state s
          JOIN dim_company_us c
            ON c.cik = s.entity_id AND c.include_in_pipeline
         WHERE {' AND '.join(where)}
         ORDER BY s.entity_id
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        return [row[0] for row in cur.fetchall()]


def download_dispatch_node(state: SECDailyState) -> dict[str, Any]:
    """Materialize the pending CIK list so fan-out can hand each bucket its own IDs."""
    if state.get("skip_download"):
        return {"download_summary": {"skipped": True}, "download_pending_ciks": []}
    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        ciks = _fetch_pending_ciks(only)
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "download_dispatch", exc)
        return {"download_summary": {"scope": 0, "buckets": 0, "error": str(exc)[:300]}, "download_pending_ciks": []}
    limit = state.get("cik_limit")
    if limit:
        ciks = ciks[: int(limit)]
    buckets = chunk_ids(ciks)
    return {
        "download_pending_ciks": [cik for chunk in buckets for cik in chunk],
        "download_summary": {"scope": sum(len(b) for b in buckets), "buckets": len(buckets)},
    }


def _fanout_download_buckets(state: SECDailyState) -> list[Send]:
    summary = state.get("download_summary") or {}
    if summary.get("skipped"):
        return [Send("xbrl_extract", dict(state))]
    pending = state.get("download_pending_ciks") or []
    if not pending:
        return [Send("xbrl_extract", dict(state))]

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
                "download_one",
                {
                    "thread_id": state["thread_id"],
                    "ciks": bucket,
                    "bucket_id": bid,
                    "bucket_index": idx,
                },
            )
        )
    return sends or [Send("xbrl_extract", dict(state))]


def download_one_node(state: dict[str, Any]) -> dict[str, Any]:
    """Run download_us_sources for one concrete bucket of CIKs."""
    from xbrl_sec.sec.pipelines.us import download_us_sources

    ciks = list(state.get("ciks") or [])
    bid = state.get("bucket_id") or bucket_id(GRAPH_NAME, state.get("thread_id", ""), int(state.get("bucket_index") or 0))
    bucket_index = int(state.get("bucket_index") or 0)
    thread_id = state.get("thread_id", "")

    ctx = log_bucket_start(GRAPH_NAME, thread_id, _BUCKET_STAGE, bucket_index, ciks, "US")
    try:
        result = download_us_sources(entity_ids=ciks, force=False, filing_types=None)
        rows_out = int((result or {}).get("ok") or 0) if isinstance(result, dict) else 0
        log_bucket_finish(ctx, "succeeded", rows_in=len(ciks), rows_out=rows_out)
        return {
            "bucket_ledger": {
                bid: {
                    "status": "succeeded",
                    "bucket_index": bucket_index,
                    "bucket_size": len(ciks),
                    "rows_in": len(ciks),
                    "rows_out": rows_out,
                    "first_id": ciks[0] if ciks else None,
                    "last_id": ciks[-1] if ciks else None,
                }
            }
        }
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300]
        log_bucket_finish(ctx, "failed", rows_in=len(ciks), rows_out=0, error=message)
        return {
            "bucket_ledger": {
                bid: {
                    "status": "failed",
                    "bucket_index": bucket_index,
                    "bucket_size": len(ciks),
                    "rows_in": len(ciks),
                    "rows_out": 0,
                    "first_id": ciks[0] if ciks else None,
                    "last_id": ciks[-1] if ciks else None,
                    "error": message,
                }
            }
        }


def xbrl_extract_node(state: SECDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.us import extract_us_xbrl

    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        rows = extract_us_xbrl(entity_ids=only, force=False)
        return {"xbrl_extract_summary": {"rows": rows or 0}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "xbrl_extract", exc)
        return {"xbrl_extract_summary": {"error": str(exc)[:300]}}


def parse_raw_node(state: SECDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.pipelines.us import parse_us_raw

    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        rows = parse_us_raw(entity_ids=only, force=False, ensure_linkbases=False, sync_index=False)
        return {"parse_raw_summary": {"rows": int(rows or 0)}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "parse_raw", exc)
        return {"parse_raw_summary": {"error": str(exc)[:300]}}


def auto_map_unknown_concepts_node(state: SECDailyState) -> dict[str, Any]:
    """Agentic loop — calls into xbrl_sec.sec.graphs.concept_mapping."""
    targets = fetch_unmapped_concepts("US", limit=_MAX_CONCEPT_MAPPING_TARGETS)
    if not targets:
        return {"concept_mapping_summary": {"candidates": 0, "auto_promoted": 0, "queued_for_review": 0, "rejected": 0}}
    try:
        result = auto_map_batch(targets, jurisdiction="US", thread_id=state.get("thread_id"))
        return {"concept_mapping_summary": result}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "auto_map_unknown_concepts", exc)
        return {"concept_mapping_summary": {"error": str(exc)[:300]}}


def standardize_node(state: SECDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.std.us_standardize import populate_us_std

    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        populate_us_std(entity_ids=only, full=False)
        return {"standardize_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "standardize", exc)
        return {"standardize_summary": {"error": str(exc)[:300]}}


def metrics_compute_node(state: SECDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.metrics.compute import compute_metrics

    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        compute_metrics("US", entity_ids=only, full=False)
        return {"metrics_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "metrics_compute", exc)
        return {"metrics_summary": {"error": str(exc)[:300]}}


def recon_node(state: SECDailyState) -> dict[str, Any]:
    from xbrl_sec.sec.metrics.recon import build_recon

    only = _normalize_ciks(state.get("only_ciks") or []) or None
    try:
        build_recon("US", entity_ids=only, full=False)
        return {"recon_summary": {"ok": True}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "recon", exc)
        return {"recon_summary": {"error": str(exc)[:300]}}


def sec_text_extract_node(state: SECDailyState) -> dict[str, Any]:
    if not state.get("extract_sections") or state.get("skip_text_extract"):
        return {"text_extract_summary": {"skipped": True}}
    filings = fetch_recent_unparsed_filings("US", limit=10)
    if not filings:
        return {"text_extract_summary": {"filings": 0}}
    try:
        result = extract_filing_sections(filings)
        return {"text_extract_summary": {"filings": len(filings), **result}}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "sec_text_extract", exc)
        return {"text_extract_summary": {"error": str(exc)[:300]}}


def quality_gate_node(state: SECDailyState) -> dict[str, Any]:
    errors = state.get("errors") or []
    concept = state.get("concept_mapping_summary") or {}
    queued_review = int(concept.get("queued_for_review") or 0)
    recon = state.get("recon_summary") or {}

    if errors or recon.get("error"):
        status = "red"
    elif queued_review > 10:
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
                "concept_mapping_summary": concept,
                "recon_summary": recon,
                "filing_diff": state.get("filing_diff_summary"),
            },
        )
        approval_request = {
            "approval_id": str(request.approval_id),
            "node": request.node_name,
        }
    return {"quality_status": status, "approval_request": approval_request}


def _route_after_quality_gate(state: SECDailyState) -> Literal["human_review", "publish"]:
    return "human_review" if state.get("quality_status") != "green" else "publish"


def human_review_node(state: SECDailyState) -> dict[str, Any]:
    request = state.get("approval_request") or {}
    decision = interrupt(
        {
            "graph": GRAPH_NAME,
            "approval_id": request.get("approval_id"),
            "thread_id": state["thread_id"],
            "summary": {
                "status": state.get("quality_status"),
                "errors": len(state.get("errors") or []),
                "concept_mapping_queued": (state.get("concept_mapping_summary") or {}).get("queued_for_review", 0),
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


def publish_node(state: SECDailyState) -> dict[str, Any]:
    return {"published_at": datetime.now(timezone.utc).isoformat()}


def build_graph() -> Any:
    g = StateGraph(SECDailyState)
    g.add_node("cik_diff", cik_diff_node)
    g.add_node("filing_diff", filing_diff_node)
    g.add_node("download_dispatch", download_dispatch_node)
    g.add_node("download_one", download_one_node)
    g.add_node("xbrl_extract", xbrl_extract_node)
    g.add_node("parse_raw", parse_raw_node)
    g.add_node("auto_map_unknown_concepts", auto_map_unknown_concepts_node)
    g.add_node("standardize", standardize_node)
    g.add_node("metrics_compute", metrics_compute_node)
    g.add_node("recon", recon_node)
    g.add_node("sec_text_extract", sec_text_extract_node)
    g.add_node("quality_gate", quality_gate_node)
    g.add_node("human_review", human_review_node)
    g.add_node("publish", publish_node)

    g.add_edge(START, "cik_diff")
    g.add_edge("cik_diff", "filing_diff")
    g.add_edge("filing_diff", "download_dispatch")
    g.add_conditional_edges(
        "download_dispatch",
        _fanout_download_buckets,
        ["download_one", "xbrl_extract"],
    )
    g.add_edge("download_one", "xbrl_extract")
    g.add_edge("xbrl_extract", "parse_raw")
    g.add_edge("parse_raw", "auto_map_unknown_concepts")
    g.add_edge("auto_map_unknown_concepts", "standardize")
    g.add_edge("standardize", "metrics_compute")
    g.add_edge("metrics_compute", "recon")
    g.add_edge("recon", "sec_text_extract")
    g.add_edge("sec_text_extract", "quality_gate")
    g.add_conditional_edges(
        "quality_gate",
        _route_after_quality_gate,
        {"human_review": "human_review", "publish": "publish"},
    )
    g.add_edge("human_review", "publish")
    g.add_edge("publish", END)
    return g


def compile_sec_daily_graph(checkpointer: Any | None = None) -> Any:
    graph = build_graph()
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def _default_initial_state(args: argparse.Namespace) -> SECDailyState:
    return SECDailyState(
        thread_id=args.thread_id,
        run_date=args.since or date.today().isoformat(),
        jurisdiction="US",
        only_ciks=args.cik or [],
        cik_limit=args.limit,
        extract_sections=args.extract_sections,
        skip_download=args.skip_download,
        skip_text_extract=not args.extract_sections,
        force_rerun=bool(getattr(args, "force_rerun", False)),
        errors=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SEC daily LangGraph pipeline")
    parser.add_argument("--thread-id", default=f"sec-daily-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--since", default=None, help="Optional ISO date to limit the diff window.")
    parser.add_argument("--cik", action="append", default=[], help="Restrict to these CIKs (repeatable).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--extract-sections", action="store_true")
    parser.add_argument("--force-rerun", action="store_true",
                        help="Ignore bucket_ledger — re-fire every download bucket even if a prior run succeeded.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_llm_cache()
    os.environ.setdefault("MZQA_PIPELINE_APP_RUN_ID", uuid.uuid4().hex)

    compiled = compile_sec_daily_graph()
    initial = _default_initial_state(args)

    if args.dry_run:
        dispatch_state = {**initial, **cik_diff_node(initial), **filing_diff_node(initial)}
        dispatch_state.update(download_dispatch_node(dispatch_state))
        pending = list(dispatch_state.get("download_pending_ciks") or [])
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
                "filing_diff": final_state.get("filing_diff_summary"),
                "parse_raw_summary": final_state.get("parse_raw_summary"),
                "concept_mapping_summary": final_state.get("concept_mapping_summary"),
                "text_extract_summary": final_state.get("text_extract_summary"),
                "published_at": final_state.get("published_at"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def resume_graph(thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    compiled = compile_sec_daily_graph()
    config = {"configurable": {"thread_id": thread_id}}
    final_state: dict[str, Any] = {}
    for event in compiled.stream(Command(resume=decision), config=config, stream_mode="values"):
        final_state = event
    return final_state


if __name__ == "__main__":
    raise SystemExit(main())
