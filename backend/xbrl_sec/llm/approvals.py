"""Persistence-Helper für LangGraph interrupt() Approval-Gates.

Wird vom Quality-Gate-Knoten aufgerufen, bevor interrupt() pausiert. Speichert
die Pending-Row in sec.pipeline_approval, schreibt mit der gleichen UUID den
thread_id-Pointer, damit der React-UI-Endpoint sie auflisten kann.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from xbrl_sec.sec.db.connection import connect


_logger = logging.getLogger("mzqa.approvals")


def _table_missing(exc: Exception) -> bool:
    """Treat 'relation does not exist' as 'no approvals yet', not as 500."""
    return 'pipeline_approval' in str(exc) and 'does not exist' in str(exc)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: UUID
    thread_id: str
    graph_name: str
    node_name: str
    payload: dict[str, Any]


def create_pending_approval(
    *,
    thread_id: str,
    graph_name: str,
    node_name: str,
    payload: dict[str, Any],
) -> ApprovalRequest:
    sql = """
        INSERT INTO sec.pipeline_approval
            (thread_id, graph_name, node_name, payload)
        VALUES (%s, %s, %s, %s::jsonb)
        RETURNING approval_id
    """
    import json as _json

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (thread_id, graph_name, node_name, _json.dumps(payload, default=str)))
        approval_id = cur.fetchone()[0]
    return ApprovalRequest(
        approval_id=approval_id,
        thread_id=thread_id,
        graph_name=graph_name,
        node_name=node_name,
        payload=payload,
    )


def list_pending_approvals(graph_name: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = """
        SELECT approval_id, thread_id, graph_name, node_name, payload, created_at, expires_at
        FROM sec.pipeline_approval
        WHERE status = 'pending'
    """
    params: list[Any] = []
    if graph_name:
        sql += " AND graph_name = %s"
        params.append(graph_name)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [
                {
                    "approval_id": str(row[0]),
                    "thread_id": row[1],
                    "graph_name": row[2],
                    "node_name": row[3],
                    "payload": row[4],
                    "created_at": row[5].isoformat() if row[5] else None,
                    "expires_at": row[6].isoformat() if row[6] else None,
                }
                for row in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001
        if _table_missing(exc):
            _logger.warning("sec.pipeline_approval missing — apply migrations 124-127")
            return []
        _logger.exception("list_pending_approvals failed: %s", exc)
        return []


def record_decision(
    *,
    approval_id: UUID | str,
    decision: dict[str, Any],
    decided_by: str | None,
    status: str = "approved",
) -> None:
    import json as _json

    sql = """
        UPDATE sec.pipeline_approval
        SET status = %s,
            decision = %s::jsonb,
            decided_by = %s,
            decided_at = NOW()
        WHERE approval_id = %s
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (status, _json.dumps(decision, default=str), decided_by, str(approval_id)))


def expire_stale_approvals() -> int:
    sql = """
        UPDATE sec.pipeline_approval
        SET status = 'expired', decided_at = NOW()
        WHERE status = 'pending' AND expires_at < NOW()
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.rowcount or 0


def find_pending_by_thread(thread_id: str) -> dict[str, Any] | None:
    sql = """
        SELECT approval_id, graph_name, node_name, payload, created_at
        FROM sec.pipeline_approval
        WHERE thread_id = %s AND status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (thread_id,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        if _table_missing(exc):
            _logger.warning("sec.pipeline_approval missing — apply migrations 124-127")
            return None
        _logger.exception("find_pending_by_thread failed: %s", exc)
        return None
    if not row:
        return None
    return {
        "approval_id": str(row[0]),
        "graph_name": row[1],
        "node_name": row[2],
        "payload": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
    }
