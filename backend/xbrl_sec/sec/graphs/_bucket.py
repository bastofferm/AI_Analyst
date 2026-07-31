"""Shared bucket mechanics for SEC/EDINET download fan-out.

Each fan-out worker receives a concrete, disjoint list of IDs; this module owns
the chunking, stable per-bucket identity, ledger reducer, and per-bucket
telemetry so both graphs stay in sync.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence

from xbrl_sec.sec.state.store import RunContext, finish_run, start_run


DOWNLOAD_BUCKET_SIZE = 25


def chunk_ids(ids: Iterable[str], size: int = DOWNLOAD_BUCKET_SIZE) -> list[list[str]]:
    """Dedupe, sort, and split ids into disjoint chunks of ``size``.

    Dedupe-before-chunk is what guarantees that no ID appears in two buckets.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in ids:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    ordered.sort()
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def bucket_id(graph_name: str, thread_id: str, index: int) -> str:
    """Stable bucket identity — identical across resume of the same thread."""
    return f"{graph_name}:{thread_id}:b{index:04d}"


def merge_bucket_ledger(
    left: dict[str, dict[str, Any]] | None,
    right: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Reducer for ``state['bucket_ledger']`` — last write wins per bucket_id."""
    merged: dict[str, dict[str, Any]] = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = value
    return merged


def _ids_hash(ids: Sequence[str]) -> str:
    h = hashlib.sha1()
    for value in ids:
        h.update(value.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:12]


def log_bucket_start(
    graph_name: str,
    thread_id: str,
    stage: str,
    bucket_index: int,
    ids: Sequence[str],
    jurisdiction: str,
) -> RunContext:
    """Open a pipeline_stage_run row for one bucket with concrete ID metadata."""
    import json

    scope = json.dumps(
        {
            "graph_name": graph_name,
            "thread_id": thread_id,
            "bucket_index": bucket_index,
            "bucket_size": len(ids),
            "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
            "ids_hash": _ids_hash(ids),
        },
        default=str,
    )
    return start_run(jurisdiction, stage, "incremental", scope=scope)


def log_bucket_finish(
    ctx: RunContext,
    status: str,
    rows_in: int,
    rows_out: int,
    error: str | None = None,
) -> None:
    finish_run(ctx, status, rows_in=rows_in, rows_out=rows_out, error=error)
