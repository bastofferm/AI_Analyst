-- 124_langgraph_checkpoint.sql
-- Backing tables for LangGraph PostgresSaver (resumable graph state).
--
-- Schema is compatible with langgraph-checkpoint-postgres 2.x: thread_id +
-- checkpoint_id form the lineage, parent_checkpoint_id chains them. metadata
-- stays small (cursors only, NEVER bulk pipeline rows).

CREATE TABLE IF NOT EXISTS sec.langgraph_checkpoint (
    thread_id             TEXT NOT NULL,
    checkpoint_ns         TEXT NOT NULL DEFAULT '',
    checkpoint_id         TEXT NOT NULL,
    parent_checkpoint_id  TEXT,
    type                  TEXT,
    checkpoint            BYTEA NOT NULL,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    ts                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_langgraph_checkpoint_thread_ts
    ON sec.langgraph_checkpoint(thread_id, ts DESC);

CREATE TABLE IF NOT EXISTS sec.langgraph_checkpoint_writes (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    checkpoint_id  TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    idx            INT NOT NULL,
    channel        TEXT NOT NULL,
    type           TEXT,
    value          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE TABLE IF NOT EXISTS sec.langgraph_checkpoint_blobs (
    thread_id      TEXT NOT NULL,
    checkpoint_ns  TEXT NOT NULL DEFAULT '',
    channel        TEXT NOT NULL,
    version        TEXT NOT NULL,
    type           TEXT,
    blob           BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

COMMENT ON TABLE sec.langgraph_checkpoint IS
  'LangGraph PostgresSaver checkpoints. Keep per-checkpoint payload <100 KB '
  '(cursors and IDs only); bulk pipeline data lives in fact tables.';
