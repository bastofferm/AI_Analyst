-- 058_macro_regime.sql
--
-- Quarterly macro regime quadrant table. One row per (jurisdiction, quarter)
-- with growth/inflation z-scores and a quadrant label. Powers the macro
-- regime scatter chart on the newsletter page.
--
-- Distinct from fact_macro_factor (PCA business cycle in business_cycle.py):
-- this table holds the *two-axis* growth-vs-inflation coordinates used by
-- the regime quadrant UI, not a single PC1 factor value.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_macro_regime (
    jurisdiction   CHAR(2)     NOT NULL,        -- 'US','JP','EZ','CH'
    period_end     DATE        NOT NULL,        -- last day of quarter
    fiscal_quarter TEXT        NOT NULL,        -- display label, e.g. 'Q1 24'
    growth_z       NUMERIC(8,4),                -- growth-momentum z-score, 8Q window
    inflation_z    NUMERIC(8,4),                -- inflation-momentum z-score, 8Q window
    quadrant       TEXT,                        -- Goldilocks|Reflation|Stagflation|Deflation
    is_current     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, period_end)
);

CREATE INDEX IF NOT EXISTS idx_macro_regime_latest
    ON fact_macro_regime (jurisdiction, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_macro_regime_current
    ON fact_macro_regime (jurisdiction)
    WHERE is_current = TRUE;

COMMENT ON TABLE fact_macro_regime IS
    'Quarterly macro-regime quadrant coordinates. Powers /api/newsletter/macro-regime. '
    'Populated by xbrl_sec.sec.sources.macro_regime_compute.';
