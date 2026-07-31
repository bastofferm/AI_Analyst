-- Investor-facing macro cycle dashboard support.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_cycle_label_override (
    override_id       BIGSERIAL PRIMARY KEY,
    jurisdiction      CHAR(2) NOT NULL,
    run_id            TEXT,
    model_family      TEXT,
    effective_start   DATE NOT NULL,
    effective_end     DATE,
    override_label    TEXT NOT NULL,
    reason            TEXT,
    source            TEXT NOT NULL DEFAULT 'manual_override',
    author            TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cycle_label_override_lookup
    ON fact_cycle_label_override (jurisdiction, model_family, run_id, effective_start, COALESCE(effective_end, DATE '9999-12-31'))
    WHERE is_active;

COMMENT ON TABLE fact_cycle_label_override IS
    'Audited investor-facing overrides for cycle labels. Raw model labels remain in fact_cycle_state_monthly.';

CREATE TABLE IF NOT EXISTS ref_cycle_metric_investor_dictionary (
    metric_id              TEXT PRIMARY KEY,
    plain_label            TEXT NOT NULL,
    driver_group           TEXT NOT NULL,
    investor_description   TEXT,
    interpretation_high    TEXT,
    interpretation_low     TEXT,
    sector_applicability   TEXT NOT NULL DEFAULT 'universal',
    warning_text           TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cycle_metric_investor_driver
    ON ref_cycle_metric_investor_dictionary (driver_group, metric_id);

COMMENT ON TABLE ref_cycle_metric_investor_dictionary IS
    'Plain-language overlay for regime-conditioned stock driver metrics.';

CREATE TABLE IF NOT EXISTS fact_cycle_ic_job_status (
    job_key                  TEXT PRIMARY KEY,
    jurisdiction             CHAR(2) NOT NULL,
    run_id                   TEXT NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'pending',
    metric_family            TEXT NOT NULL DEFAULT 'all',
    horizons_json            JSONB NOT NULL DEFAULT '[]'::jsonb,
    chunk_size               INTEGER NOT NULL DEFAULT 25,
    total_metrics            INTEGER NOT NULL DEFAULT 0,
    completed_metrics        INTEGER NOT NULL DEFAULT 0,
    failed_metrics           INTEGER NOT NULL DEFAULT 0,
    rows_written             INTEGER NOT NULL DEFAULT 0,
    hard_rows_written        INTEGER NOT NULL DEFAULT 0,
    probability_rows_written INTEGER NOT NULL DEFAULT 0,
    state_start              DATE,
    state_end                DATE,
    started_at               TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at             TIMESTAMPTZ,
    elapsed_seconds          DOUBLE PRECISION,
    diagnostics_json         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_cycle_ic_job_status_lookup
    ON fact_cycle_ic_job_status (jurisdiction, run_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS fact_cycle_ic_job_metric_status (
    job_key       TEXT NOT NULL REFERENCES fact_cycle_ic_job_status (job_key) ON DELETE CASCADE,
    metric_id     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    rows_written  INTEGER NOT NULL DEFAULT 0,
    error_text    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_key, metric_id)
);

CREATE INDEX IF NOT EXISTS idx_cycle_ic_job_metric_status_lookup
    ON fact_cycle_ic_job_metric_status (job_key, status, metric_id);

COMMENT ON TABLE fact_cycle_ic_job_status IS
    'Progress and resumability state for long-running regime-conditioned IC recomputations.';
