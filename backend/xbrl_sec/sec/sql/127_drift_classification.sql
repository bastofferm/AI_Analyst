-- 127_drift_classification.sql
-- Audit table for cross-source recon outcomes (e.g. SEC vs EDINET for dual-listed
-- filers like Toyota 7203 / TM ADR). Populated by the drift_explain_agent node
-- in the EDINET LangGraph (Phase 4). Created in Phase 3 so the SEC graph can
-- already record concept-level drift findings during reconciliation.

CREATE TABLE IF NOT EXISTS sec.drift_classification (
    drift_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cik             TEXT,
    edinet_code     TEXT,
    period_end      DATE NOT NULL,
    concept         TEXT NOT NULL,
    reason          TEXT NOT NULL
        CHECK (reason IN (
            'fx_translation', 'period_difference', 'accounting_standard_difference',
            'scope_difference', 'data_quality_issue', 'unexplained'
        )),
    action          TEXT NOT NULL
        CHECK (action IN ('auto_accept', 'auto_correct', 'human_review', 'halt_pipeline')),
    confidence      NUMERIC(5,4) NOT NULL,
    rationale       TEXT,
    fx_rate_used    NUMERIC(20,8),
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cik, edinet_code, period_end, concept)
);

CREATE INDEX IF NOT EXISTS idx_drift_classification_pair
    ON sec.drift_classification(cik, edinet_code, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_drift_classification_action
    ON sec.drift_classification(action, decided_at DESC);

COMMENT ON TABLE sec.drift_classification IS
  'Cross-source reconciliation audit. One row per (filer pair, period, concept). '
  'Drives the human-review queue and the auto-correction trail.';
