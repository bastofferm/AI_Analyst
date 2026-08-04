-- 134_quant_alpha_training.sql
-- Quant alpha-model training support:
--   (1) Backfill `importance` on fact_metrics_intl from ref_metric_definitions so the
--       cross-sectional feature selection (`_load_metric_ids`, importance<=2) works for INTL
--       exactly as it does for US/JP. importance is a global per-metric_id property, so this
--       is a straight join — no re-derivation. Idempotent.
--   (2) Training/coverage ledger the parallel training CLI (api.quant.qlib_train_all) writes:
--       one row per market/country model, and one row per firm documenting the latest training
--       + the quarterly next-due date. Mirrors the fact_cycle_ic_job_status status-table pattern.

-- (1) INTL importance backfill -------------------------------------------------------------
UPDATE fact_metrics_intl m
SET    importance = r.importance
FROM   ref_metric_definitions r
WHERE  m.metric_id = r.metric_id
  AND  m.importance IS DISTINCT FROM r.importance;

-- (2a) per-model registry ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant_alpha_model (
    model_key        TEXT PRIMARY KEY,              -- 'US', 'JP', 'INTL:DE', ...
    jurisdiction     TEXT NOT NULL,                 -- 'US' | 'JP' | 'INTL'
    country_code     TEXT,                          -- NULL for US/JP, ISO-2 for INTL
    label            TEXT NOT NULL,                 -- forward_1m | forward_3m
    version          TEXT NOT NULL,                 -- artifact trained_at (ISO)
    trained_at       TIMESTAMPTZ NOT NULL,
    train_start      DATE,
    train_end        DATE,
    rank_ic          DOUBLE PRECISION,
    n_train_names    INTEGER,
    coverage_count   INTEGER,                       -- names in the latest cross-section
    status           TEXT NOT NULL DEFAULT 'trained',
    next_due         DATE,                          -- trained_at + 3 months (quarterly cadence)
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    diagnostics_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- (2b) per-firm coverage ledger ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant_alpha_coverage (
    jurisdiction     TEXT NOT NULL,                 -- 'US' | 'JP' | 'INTL'
    ticker           TEXT NOT NULL,
    model_key        TEXT NOT NULL,                 -- which model covers this firm
    country_code     TEXT,
    last_as_of       DATE,                          -- month of the latest scored cross-section
    expected_return  DOUBLE PRECISION,              -- monthly, from the latest cross-section
    covered          BOOLEAN NOT NULL DEFAULT TRUE,
    last_trained_at  TIMESTAMPTZ,
    next_due         DATE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, ticker)
);

CREATE INDEX IF NOT EXISTS quant_alpha_coverage_model_idx ON quant_alpha_coverage (model_key);
