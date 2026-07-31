"""FastAPI router for LangGraph human-approval gates.

Exposes:
  GET  /api/pipeline/approvals               — list pending approvals
  POST /api/pipeline/{thread_id}/resume      — submit decision, resume the graph

The endpoint reads the pending row from sec.pipeline_approval, writes the
decision back, and then re-invokes the compiled graph with Command(resume=...).
Resumption happens synchronously: the React UI gets the final state in the
response.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from xbrl_sec.etf.graphs.daily import resume_graph as resume_etf_daily_graph
from xbrl_sec.llm.approvals import (
    find_pending_by_thread,
    list_pending_approvals,
    record_decision,
)
from xbrl_sec.sec.graphs.edinet_daily import resume_graph as resume_edinet_daily_graph
from xbrl_sec.sec.graphs.sec_daily import resume_graph as resume_sec_daily_graph


_RESUMERS = {
    "etf_daily": resume_etf_daily_graph,
    "sec_daily": resume_sec_daily_graph,
    "edinet_daily": resume_edinet_daily_graph,
}


router = APIRouter()


class ApprovalDecisionRequest(BaseModel):
    approve: bool = Field(default=True, description="True to allow commit, False to halt.")
    notes: str | None = Field(default=None, max_length=1000)
    decided_by: str | None = Field(default=None, max_length=80)
    graph: Literal["etf_daily", "sec_daily", "edinet_daily"] = Field(
        default="etf_daily",
        description="Which graph to resume. etf_daily (Phase 2), sec_daily (Phase 3), edinet_daily (Phase 4).",
    )


class PendingApproval(BaseModel):
    approval_id: str
    thread_id: str
    graph_name: str
    node_name: str
    payload: dict[str, Any]
    created_at: str | None
    expires_at: str | None


@router.get("/approvals", response_model=list[PendingApproval])
def list_approvals(
    graph: str | None = Query(default=None, description="Filter by graph_name."),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PendingApproval]:
    return [PendingApproval(**row) for row in list_pending_approvals(graph, limit=limit)]


@router.post("/{thread_id}/resume")
def resume_thread(thread_id: str, request: ApprovalDecisionRequest) -> dict[str, Any]:
    pending = find_pending_by_thread(thread_id)
    if not pending:
        raise HTTPException(status_code=404, detail="no pending approval for thread")

    decision_payload = {
        "approve": request.approve,
        "notes": request.notes,
        "decided_by": request.decided_by,
    }
    try:
        record_decision(
            approval_id=pending["approval_id"],
            decision=decision_payload,
            decided_by=request.decided_by,
            status="approved" if request.approve else "rejected",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"failed to record decision: {exc}") from exc

    resumer = _RESUMERS.get(request.graph)
    if resumer is None:
        raise HTTPException(status_code=400, detail=f"unsupported graph: {request.graph}")

    try:
        final_state = resumer(thread_id, decision_payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"failed to resume graph: {exc}") from exc

    return {
        "thread_id": thread_id,
        "approval_id": pending["approval_id"],
        "decision": decision_payload,
        "final_state": {
            "quality_status": final_state.get("quality_status"),
            "committed_at": final_state.get("committed_at"),
            "errors": final_state.get("errors"),
        },
    }
