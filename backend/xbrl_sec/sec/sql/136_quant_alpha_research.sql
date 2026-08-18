-- 136_quant_alpha_research.sql
-- Ledger for the agentic alpha-research loop (api.quant.research).
--
-- The one-shot trainer (migration 134/135) records only the OUTCOME of a training run:
-- one row per (model_key, label) holding the rank-IC of whatever it produced. The research
-- loop needs the opposite — the whole search — because the deliverable is the reasoning,
-- not just the winner: which specs were tried, what each scored across the full quality
-- battery, what the Model Validation unit objected to, and why the champion was or was not
-- promoted.
--
-- Two tables, mirroring the fact_cycle_ic_job_status pattern: a run header the UI polls,
-- and one row per iteration carrying that round's complete validation report. Both are
-- also created idempotently by api.quant.research.runner._ensure_tables, so the feature
-- works before this migration is applied.

-- (1) run header -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant_alpha_research_run (
    run_id             TEXT PRIMARY KEY,
    model_key          TEXT NOT NULL,              -- 'US', 'JP', 'INTL:DE', ...
    jurisdiction       TEXT NOT NULL,
    label              TEXT NOT NULL,              -- forward_1m | 3m | 6m | 12m
    status             TEXT NOT NULL,              -- queued|running|complete|failed|cancelled
    provider           TEXT,                       -- llm_providers id for the 3 desk agents
    advisor_provider   TEXT,                       -- deliberately a DIFFERENT provider
    max_iterations     INTEGER NOT NULL DEFAULT 4,
    iterations_done    INTEGER NOT NULL DEFAULT 0,
    current_stage      TEXT,                       -- live node name, for the UI's stepper
    baseline_json      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- the incumbent it must beat
    champion_iteration INTEGER,
    champion_kind      TEXT,                       -- single | ensemble
    champion_score     DOUBLE PRECISION,
    promoted           BOOLEAN NOT NULL DEFAULT FALSE,
    promotion_reason   TEXT,
    stop_reason        TEXT,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    elapsed_seconds    DOUBLE PRECISION,
    error              TEXT,
    summary_json       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS quant_alpha_research_run_lookup_idx
    ON quant_alpha_research_run (model_key, label, started_at DESC);
CREATE INDEX IF NOT EXISTS quant_alpha_research_run_status_idx
    ON quant_alpha_research_run (status);

-- (2) one row per iteration --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant_alpha_research_iteration (
    run_id            TEXT NOT NULL,
    iteration         INTEGER NOT NULL,
    spec_json         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- the full TrainingSpec used
    spec_hash         TEXT,                                  -- reproducibility key
    patch_json        JSONB NOT NULL DEFAULT '{}'::jsonb,    -- changes + rejected keys
    metrics_json      JSONB NOT NULL DEFAULT '{}'::jsonb,    -- the quality-attribute battery
    rating_json       JSONB NOT NULL DEFAULT '{}'::jsonb,    -- perturbation robustness rating
    breakdown_json    JSONB NOT NULL DEFAULT '{}'::jsonb,    -- GICS + FF-exposure tables
    validation_json   JSONB NOT NULL DEFAULT '{}'::jsonb,    -- Model Validation verdict
    pm_json           JSONB NOT NULL DEFAULT '{}'::jsonb,    -- Portfolio Manager verdict
    advisor_json      JSONB NOT NULL DEFAULT '{}'::jsonb,    -- External Advisor note
    researcher_json   JSONB NOT NULL DEFAULT '{}'::jsonb,    -- the proposal for the next round
    report_json       JSONB NOT NULL DEFAULT '{}'::jsonb,    -- the full report (UI + PDF)
    artifact_path     TEXT,
    elapsed_seconds   DOUBLE PRECISION,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, iteration)
);

CREATE INDEX IF NOT EXISTS quant_alpha_research_iteration_run_idx
    ON quant_alpha_research_iteration (run_id);

COMMENT ON TABLE quant_alpha_research_run IS
    'One agentic alpha-research run: the search, its champion, and the promotion decision.';
COMMENT ON TABLE quant_alpha_research_iteration IS
    'One round of an agentic research run: the spec tried, its full model validation report, and the four agents'' verdicts.';
