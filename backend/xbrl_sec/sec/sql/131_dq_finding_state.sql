-- Run-over-run persistence for committee data-quality findings.
--
-- The committee DQ/mapping agent (ai_analyst.dq_triage.record_findings) upserts each
-- deterministic finding here keyed by its stable dq- id, so the agent can show
-- new/resolved deltas across runs and mark benign definition differences as "explained"
-- (so they stop ranking above real issues). This table is advisory only: it is written
-- by the committee agent and NEVER gates a run or mutates raw/standardized/mapping data.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dq_finding_state (
    finding_id       TEXT PRIMARY KEY,          -- stable_dq_id(...) from data_quality_agent
    ticker           TEXT NOT NULL,
    jurisdiction     TEXT,
    entity_id        TEXT,
    layer            TEXT,                       -- raw | standardized | metrics | recon | yahoo_cross_check
    severity         TEXT,                       -- info | low | medium | high | blocker
    title            TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'explained', 'resolved')),
    explained_reason TEXT,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dq_finding_state_ticker
    ON dq_finding_state (ticker, status);

COMMENT ON TABLE dq_finding_state IS
    'Run-over-run persistence of committee data-quality findings (stable dq- ids). Advisory only: written by the committee DQ/mapping agent to compute new/resolved deltas and mark benign findings explained; never gates a run.';
