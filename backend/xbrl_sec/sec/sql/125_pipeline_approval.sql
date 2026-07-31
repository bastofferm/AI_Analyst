-- 125_pipeline_approval.sql
-- Persistence layer for LangGraph interrupt()-driven human approval gates.
--
-- A pipeline_approval row is created when a graph node calls interrupt(...).
-- The React dataPipelineApp polls status='pending' rows, presents a UI, and
-- POSTs the decision to /api/pipeline/{thread_id}/resume. The handler then
-- calls graph.invoke(Command(resume=decision)) which lifts the interrupt.
-- An hourly job auto-expires rows older than 24h.

CREATE TABLE IF NOT EXISTS sec.pipeline_approval (
    approval_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id      TEXT NOT NULL,
    graph_name     TEXT NOT NULL,
    node_name      TEXT NOT NULL,
    payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decision       JSONB,
    decided_by     TEXT,
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at     TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_pipeline_approval_pending
    ON sec.pipeline_approval(graph_name, created_at DESC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_pipeline_approval_thread
    ON sec.pipeline_approval(thread_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_approval_expires
    ON sec.pipeline_approval(expires_at)
    WHERE status = 'pending';

COMMENT ON TABLE sec.pipeline_approval IS
  'Human approval queue for LangGraph interrupt() pauses. One row per pause; '
  'lifetime is at most 24h before auto-expire. Decision JSONB is fed back to '
  'graph.invoke(Command(resume=...)).';
