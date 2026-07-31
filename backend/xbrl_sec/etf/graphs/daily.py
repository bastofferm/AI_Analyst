"""ETF Daily Ingestion Graph (Phase 2 Pilot).

LangGraph-Implementierung der drei bestehenden Stages plus die neuen agentischen
Knoten:

    firds_discover → yahoo_resolve → prices_fetch → holdings_dispatch
    → holdings_per_provider (Send-fanout) → profile_enrich
    → classify_unknown_providers → holdings_anomaly_detector
    → bond_ratings_multi_source → quality_gate
    → [human_approval bei Low-Confidence] → commit

State enthält absichtlich nur Cursors, Zähler und IDs — keine Bulk-Daten. Die
eigentlichen Pipeline-Writes passieren in den existierenden Modulen
(xbrl_sec/etf/pipeline.py, xbrl_sec/etf/holdings/service.py), die hier
wiederverwendet werden.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal, TypedDict

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Command, Send, interrupt

from xbrl_sec.etf import pipeline as etf_pipeline
from xbrl_sec.etf.bond_ratings import resolve_bond_rating
from xbrl_sec.etf.graphs.anomaly import detect_holdings_anomalies
from xbrl_sec.etf.holdings.service import run_holdings_fetch
from xbrl_sec.etf.providers import seed_provider_registry
from xbrl_sec.llm import setup_llm_cache
from xbrl_sec.llm.approvals import create_pending_approval, record_decision
from xbrl_sec.sec.db.connection import connect


GRAPH_NAME = "etf_daily"


class ETFDailyState(TypedDict, total=False):
    thread_id: str
    run_date: str
    only_isins: list[str]
    isin_limit: int | None
    skip_firds: bool
    skip_prices: bool
    skip_holdings: bool
    skip_bond_ratings: bool
    extract_sections: bool

    # Cursors + light summaries (no bulk data, target < 100 KB per checkpoint)
    firds: dict[str, Any]
    yahoo_resolver: dict[str, Any]
    prices: dict[str, Any]
    holdings: dict[str, Any]
    providers_per_run: list[str]
    profile: dict[str, Any]
    classification_summary: dict[str, Any]
    anomalies: list[dict[str, Any]]
    bond_ratings_summary: dict[str, Any]
    quality_status: Literal["green", "yellow", "red"]
    approval_request: dict[str, Any] | None
    approval_decision: dict[str, Any] | None
    errors: list[dict[str, str]]
    committed_at: str | None


def _record_error(state: ETFDailyState, stage: str, exc: Exception) -> None:
    errors = list(state.get("errors") or [])
    errors.append({"stage": stage, "type": exc.__class__.__name__, "message": str(exc)[:300]})
    state["errors"] = errors


def firds_discover_node(state: ETFDailyState) -> dict[str, Any]:
    if state.get("skip_firds"):
        return {"firds": {"skipped": True}}
    try:
        result = etf_pipeline.run_firds()
        return {"firds": result}
    except Exception as exc:  # noqa: BLE001 - recorded, pipeline continues
        _record_error(state, "firds_discover", exc)
        return {"firds": {"error": str(exc)[:300]}}


def yahoo_resolve_node(state: ETFDailyState) -> dict[str, Any]:
    """Best-effort: nur Telemetrie, der Resolver läuft separat als CLI."""
    sql = """
        SELECT COUNT(*) FROM sec.dim_etf_yahoo_symbol_candidate
        WHERE evaluated_at >= NOW() - INTERVAL '1 day'
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            recent = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "yahoo_resolve", exc)
        recent = 0
    return {"yahoo_resolver": {"candidates_evaluated_24h": recent}}


def prices_fetch_node(state: ETFDailyState) -> dict[str, Any]:
    if state.get("skip_prices"):
        return {"prices": {"skipped": True}}
    only = state.get("only_isins") or []
    limit = state.get("isin_limit")
    try:
        if only:
            # ETF pipeline.run_prices already supports targeted single ISIN; loop over
            # the provided list for multi-ISIN dry runs.
            aggregate = {"requested": 0, "ok": 0, "empty": 0, "failed": 0, "rows": 0}
            for isin in only:
                result = etf_pipeline.run_prices(isin=isin)
                for key in aggregate:
                    aggregate[key] += int(result.get(key, 0) or 0)
            return {"prices": aggregate}
        return {"prices": etf_pipeline.run_prices(limit=limit)}
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "prices_fetch", exc)
        return {"prices": {"error": str(exc)[:300]}}


def holdings_dispatch_node(state: ETFDailyState) -> dict[str, Any]:
    """Resolves the active provider list. The Send-conditional edge fans out
    one node call per provider so each runs independently in the checkpoint."""
    if state.get("skip_holdings"):
        return {"providers_per_run": []}
    sql = """
        SELECT DISTINCT d.provider_id
        FROM sec.dim_etf d
        WHERE COALESCE(d.is_active, TRUE)
          AND d.provider_id IS NOT NULL
          AND d.provider_id <> 'unknown_provider'
        ORDER BY d.provider_id
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            providers = [row[0] for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "holdings_dispatch", exc)
        providers = []
    return {"providers_per_run": providers}


def _fanout_to_providers(state: ETFDailyState) -> list[Send]:
    providers = state.get("providers_per_run") or []
    if not providers:
        return [Send("profile_enrich", state)]
    return [
        Send("holdings_per_provider", {**state, "_current_provider": provider})
        for provider in providers
    ]


def holdings_per_provider_node(state: dict[str, Any]) -> dict[str, Any]:
    provider = state.get("_current_provider")
    if not provider:
        return {"holdings": state.get("holdings") or {}}
    try:
        result = run_holdings_fetch(provider=provider, limit=state.get("isin_limit"))
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)[:300]}
    aggregate = dict(state.get("holdings") or {})
    aggregate.setdefault("per_provider", {})[provider] = result
    aggregate["total_success"] = sum(
        int(v.get("success", 0) or 0) for v in aggregate["per_provider"].values()
    )
    aggregate["total_failed"] = sum(
        int(v.get("failed", 0) or 0) for v in aggregate["per_provider"].values()
    )
    return {"holdings": aggregate}


def profile_enrich_node(state: ETFDailyState) -> dict[str, Any]:
    """Profile enrichment is a heavy yfinance pass; we only report the queue size
    so the daily graph runs predictable; full enrichment runs in a weekly job."""
    sql = """
        SELECT COUNT(*) FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE COALESCE(d.is_active, TRUE)
          AND (p.isin IS NULL OR p.profile_status IN ('pending', 'failed'))
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            pending = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "profile_enrich", exc)
        pending = -1
    return {"profile": {"pending": pending}}


def classify_unknown_providers_node(state: ETFDailyState) -> dict[str, Any]:
    """Counts how many funds remain unclassified — the actual LangChain chain
    runs out-of-band via tools/classify_unknown_etf_providers_deepseek.py."""
    sql = """
        SELECT COUNT(*) FROM sec.dim_etf
        WHERE provider_id IS NULL OR provider_id = 'unknown_provider'
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            unknown = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "classify_unknown_providers", exc)
        unknown = -1
    return {"classification_summary": {"unknown_remaining": unknown}}


def holdings_anomaly_node(state: ETFDailyState) -> dict[str, Any]:
    if state.get("skip_holdings"):
        return {"anomalies": []}
    sql = """
        SELECT DISTINCT isin FROM sec.etf_holding
        WHERE as_of_date >= CURRENT_DATE - INTERVAL '7 days'
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            recent_isins = [row[0] for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "holdings_anomaly", exc)
        return {"anomalies": []}
    findings = detect_holdings_anomalies(recent_isins)
    return {"anomalies": [f.model_dump() for f in findings]}


def bond_ratings_node(state: ETFDailyState) -> dict[str, Any]:
    if state.get("skip_bond_ratings"):
        return {"bond_ratings_summary": {"skipped": True}}
    sql = """
        SELECT d.isin, d.full_name, d.issuer_name
        FROM sec.dim_etf d
        LEFT JOIN sec.dim_etf_profile p ON p.isin = d.isin
        WHERE COALESCE(d.is_active, TRUE)
          AND COALESCE(p.bond_portfolio_pct, 0) > 0.5
        ORDER BY d.isin
        LIMIT 100
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        _record_error(state, "bond_ratings", exc)
        return {"bond_ratings_summary": {"error": str(exc)[:300]}}

    resolved = 0
    warn = 0
    none = 0
    for isin, full_name, issuer_name in rows:
        result = resolve_bond_rating(
            isin,
            fund_name=full_name,
            issuer_name=issuer_name,
            use_llm_fallback=True,
        )
        if result.rating:
            resolved += 1
            if result.confidence_warn:
                warn += 1
        else:
            none += 1
    return {
        "bond_ratings_summary": {
            "candidates": len(rows),
            "resolved": resolved,
            "warned": warn,
            "missing": none,
        }
    }


def quality_gate_node(state: ETFDailyState) -> dict[str, Any]:
    """Aggregate findings into a green / yellow / red status."""
    errors = state.get("errors") or []
    anomalies = state.get("anomalies") or []
    bond_summary = state.get("bond_ratings_summary") or {}

    high_anomalies = [a for a in anomalies if a.get("severity") == "high"]
    medium_anomalies = [a for a in anomalies if a.get("severity") == "medium"]
    bond_missing = int(bond_summary.get("missing") or 0)
    bond_warn = int(bond_summary.get("warned") or 0)

    if errors or high_anomalies or bond_missing > 25:
        status = "red"
    elif medium_anomalies or bond_warn > 0:
        status = "yellow"
    else:
        status = "green"

    needs_approval = status != "green"
    approval_request: dict[str, Any] | None = None
    if needs_approval:
        request = create_pending_approval(
            thread_id=state["thread_id"],
            graph_name=GRAPH_NAME,
            node_name="quality_gate",
            payload={
                "status": status,
                "errors": errors,
                "anomalies": anomalies,
                "bond_ratings_summary": bond_summary,
                "firds": state.get("firds"),
                "prices": state.get("prices"),
                "holdings": state.get("holdings"),
            },
        )
        approval_request = {
            "approval_id": str(request.approval_id),
            "node": request.node_name,
        }
    return {"quality_status": status, "approval_request": approval_request}


def _route_after_quality_gate(state: ETFDailyState) -> Literal["human_approval", "commit"]:
    return "human_approval" if state.get("quality_status") != "green" else "commit"


def human_approval_node(state: ETFDailyState) -> dict[str, Any]:
    """Pause the graph until an operator decides via the React UI."""
    request = state.get("approval_request") or {}
    decision = interrupt(
        {
            "graph": GRAPH_NAME,
            "approval_id": request.get("approval_id"),
            "thread_id": state["thread_id"],
            "summary": {
                "status": state.get("quality_status"),
                "anomalies": len(state.get("anomalies") or []),
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


def commit_node(state: ETFDailyState) -> dict[str, Any]:
    """Final marker — pipeline writes already happened in upstream nodes."""
    return {"committed_at": datetime.now(timezone.utc).isoformat()}


def build_graph() -> Any:
    g = StateGraph(ETFDailyState)
    g.add_node("firds_discover", firds_discover_node)
    g.add_node("yahoo_resolve", yahoo_resolve_node)
    g.add_node("prices_fetch", prices_fetch_node)
    g.add_node("holdings_dispatch", holdings_dispatch_node)
    g.add_node("holdings_per_provider", holdings_per_provider_node)
    g.add_node("profile_enrich", profile_enrich_node)
    g.add_node("classify_unknown_providers", classify_unknown_providers_node)
    g.add_node("holdings_anomaly", holdings_anomaly_node)
    g.add_node("bond_ratings", bond_ratings_node)
    g.add_node("quality_gate", quality_gate_node)
    g.add_node("human_approval", human_approval_node)
    g.add_node("commit", commit_node)

    g.add_edge(START, "firds_discover")
    g.add_edge("firds_discover", "yahoo_resolve")
    g.add_edge("yahoo_resolve", "prices_fetch")
    g.add_edge("prices_fetch", "holdings_dispatch")
    g.add_conditional_edges(
        "holdings_dispatch",
        _fanout_to_providers,
        ["holdings_per_provider", "profile_enrich"],
    )
    g.add_edge("holdings_per_provider", "profile_enrich")
    g.add_edge("profile_enrich", "classify_unknown_providers")
    g.add_edge("classify_unknown_providers", "holdings_anomaly")
    g.add_edge("holdings_anomaly", "bond_ratings")
    g.add_edge("bond_ratings", "quality_gate")
    g.add_conditional_edges(
        "quality_gate",
        _route_after_quality_gate,
        {"human_approval": "human_approval", "commit": "commit"},
    )
    g.add_edge("human_approval", "commit")
    g.add_edge("commit", END)
    return g


def compile_etf_daily_graph(checkpointer: Any | None = None) -> Any:
    graph = build_graph()
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def _default_initial_state(args: argparse.Namespace) -> ETFDailyState:
    return ETFDailyState(
        thread_id=args.thread_id,
        run_date=args.run_date or date.today().isoformat(),
        only_isins=args.isin or [],
        isin_limit=args.limit,
        skip_firds=args.skip_firds,
        skip_prices=args.skip_prices,
        skip_holdings=args.skip_holdings,
        skip_bond_ratings=args.skip_bond_ratings,
        extract_sections=False,
        errors=[],
        anomalies=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ETF daily LangGraph pipeline")
    parser.add_argument("--thread-id", default=f"etf-daily-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--isin", action="append", default=[], help="Restrict prices_fetch to these ISINs (repeatable).")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-firds", action="store_true")
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--skip-holdings", action="store_true")
    parser.add_argument("--skip-bond-ratings", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Compile the graph and report node order; do not invoke.")
    args = parser.parse_args()

    seed_provider_registry()
    setup_llm_cache()
    os.environ.setdefault("MZQA_PIPELINE_APP_RUN_ID", uuid.uuid4().hex)

    compiled = compile_etf_daily_graph()
    initial = _default_initial_state(args)

    if args.dry_run:
        nodes = sorted(compiled.get_graph().nodes)
        print(json.dumps({"dry_run": True, "thread_id": args.thread_id, "nodes": nodes}, ensure_ascii=False))
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
                "firds": final_state.get("firds"),
                "prices": final_state.get("prices"),
                "holdings": final_state.get("holdings"),
                "anomalies": len(final_state.get("anomalies") or []),
                "bond_ratings_summary": final_state.get("bond_ratings_summary"),
                "committed_at": final_state.get("committed_at"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def resume_graph(thread_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """Helper for the FastAPI approval-resume endpoint."""
    compiled = compile_etf_daily_graph()
    config = {"configurable": {"thread_id": thread_id}}
    final_state: dict[str, Any] = {}
    for event in compiled.stream(Command(resume=decision), config=config, stream_mode="values"):
        final_state = event
    return final_state


if __name__ == "__main__":
    raise SystemExit(main())
