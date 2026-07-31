"""FastAPI router for LangGraph metadata + manual triggering.

Exposes:
  GET  /api/pipeline/graphs                          — list of graphs + cron schedule
  GET  /api/pipeline/graphs/{name}/mermaid           — mermaid + ascii + node list
  POST /api/pipeline/graphs/{name}/trigger           — spawn subprocess, return thread_id + pid

The trigger runs the graph in an isolated subprocess so long-running work does
not block the API event loop. Thread IDs are returned so the UI can poll the
Approvals tab once the graph reaches its quality gate.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


router = APIRouter()


_MZQA_ROOT = Path(__file__).resolve().parents[2]


class GraphInfo(BaseModel):
    name: str
    label: str
    description: str
    module: str
    cli_flags: list[str]
    cron: str | None
    next_run: str | None
    node_count: int


class MermaidResponse(BaseModel):
    name: str
    mermaid: str
    ascii: str | None
    nodes: list[str]
    edges: list[dict[str, str]]


class TriggerRequest(BaseModel):
    thread_id: str | None = Field(default=None, description="Override the auto-generated thread id.")
    extra_args: list[str] = Field(default_factory=list, description="Additional CLI flags, e.g. ['--limit','10'].")


class TriggerResponse(BaseModel):
    graph: str
    thread_id: str
    pid: int
    argv: list[str]
    started_at: str


_GRAPHS: dict[str, dict[str, Any]] = {
    "etf_daily": {
        "label": "ETF Daily",
        "module": "xbrl_sec.etf.graphs.daily",
        "description": (
            "FIRDS → Yahoo → Prices → Holdings → Profile → Provider-Classification → "
            "Anomaly-Detector → Bond-Ratings → Quality-Gate → Approval."
        ),
        "cli_flags": [
            "--skip-firds",
            "--skip-prices",
            "--skip-holdings",
            "--skip-bond-ratings",
            "--limit N",
        ],
        "cron": "03:00 UTC (FIRDS), 22:00 UTC (Prices), Sun 04:00 (Holdings), Sat 06:00 (Ratings)",
    },
    "sec_daily": {
        "label": "SEC Daily",
        "module": "xbrl_sec.sec.graphs.sec_daily",
        "description": (
            "CIK-Diff → Filing-Diff → Download-Fanout → Parse → Auto-Map-Concepts → "
            "Standardize → Metrics → Recon → Quality-Gate."
        ),
        "cli_flags": ["--skip-download", "--extract-sections", "--limit N"],
        "cron": "23:00 UTC daily, Sun 02:00 UTC (Text-Extract)",
    },
    "edinet_daily": {
        "label": "EDINET Daily",
        "module": "xbrl_sec.sec.graphs.edinet_daily",
        "description": (
            "Master-Sync → Filing-Diff → Download-Fanout → PublicDoc-Extract → Parse → "
            "GICS (LLM-Fallback) → ISIN → Standardize → Cross-Source-Recon → Drift-Explain."
        ),
        "cli_flags": ["--skip-download", "--skip-drift", "--limit N"],
        "cron": "02:00 UTC daily",
    },
}


def _compile(name: str):
    if name == "etf_daily":
        from xbrl_sec.etf.graphs.daily import compile_etf_daily_graph

        return compile_etf_daily_graph()
    if name == "sec_daily":
        from xbrl_sec.sec.graphs.sec_daily import compile_sec_daily_graph

        return compile_sec_daily_graph()
    if name == "edinet_daily":
        from xbrl_sec.sec.graphs.edinet_daily import compile_edinet_daily_graph

        return compile_edinet_daily_graph()
    raise HTTPException(status_code=404, detail=f"unknown graph: {name}")


def _describe_graph(name: str, meta: dict[str, Any]) -> GraphInfo:
    try:
        compiled = _compile(name)
        graph = compiled.get_graph()
        node_count = len(list(graph.nodes)) - 2  # exclude __start__, __end__
    except Exception:
        node_count = -1
    return GraphInfo(
        name=name,
        label=meta["label"],
        description=meta["description"],
        module=meta["module"],
        cli_flags=meta["cli_flags"],
        cron=meta.get("cron"),
        next_run=meta.get("next_run"),
        node_count=node_count,
    )


@router.get("/graphs", response_model=list[GraphInfo])
def list_graphs() -> list[GraphInfo]:
    return [_describe_graph(name, meta) for name, meta in _GRAPHS.items()]


@router.get("/graphs/{name}/mermaid", response_model=MermaidResponse)
def graph_mermaid(name: str) -> MermaidResponse:
    if name not in _GRAPHS:
        raise HTTPException(status_code=404, detail=f"unknown graph: {name}")
    compiled = _compile(name)
    graph = compiled.get_graph()
    try:
        mermaid = graph.draw_mermaid()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"mermaid render failed: {exc}") from exc
    try:
        ascii_art = graph.draw_ascii()
    except Exception:
        ascii_art = None
    edges = []
    for edge in graph.edges:
        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)
        if source and target:
            edges.append({"source": str(source), "target": str(target)})
    return MermaidResponse(
        name=name,
        mermaid=mermaid,
        ascii=ascii_art,
        nodes=sorted(str(n) for n in graph.nodes),
        edges=edges,
    )


@router.post("/graphs/{name}/trigger", response_model=TriggerResponse)
def trigger_graph(name: str, request: TriggerRequest) -> TriggerResponse:
    if name not in _GRAPHS:
        raise HTTPException(status_code=404, detail=f"unknown graph: {name}")
    meta = _GRAPHS[name]
    thread_id = request.thread_id or f"{name}-manual-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    argv = [
        sys.executable,
        "-m",
        meta["module"],
        "--thread-id",
        thread_id,
    ]
    if request.extra_args:
        argv.extend(request.extra_args)

    log_dir = _MZQA_ROOT / "logs" / "graph_triggers"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{thread_id}.stdout.log"
    stderr_path = log_dir / f"{thread_id}.stderr.log"

    env = os.environ.copy()
    env.setdefault("MZQA_ROOT", str(_MZQA_ROOT))
    env.setdefault("PYTHONPATH", str(_MZQA_ROOT))

    try:
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdout=stdout_path.open("wb"),
            stderr=stderr_path.open("wb"),
            cwd=str(_MZQA_ROOT),
            env=env,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}") from exc

    return TriggerResponse(
        graph=name,
        thread_id=thread_id,
        pid=process.pid,
        argv=argv,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
