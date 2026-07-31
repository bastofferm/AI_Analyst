-- Lean persistence for full-scale US/JP business-cycle modeling.
--
-- Four durable grains only:
--   1) monthly point-in-time features
--   2) model runs
--   3) monthly model states
--   4) regime-conditioned equity factor ICs
--
-- Model-specific internals (transition matrices, validation bundles,
-- contribution diagnostics, VAE metadata) stay in JSON or external artifacts.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_cycle_feature_monthly (
    date              DATE NOT NULL,
    jurisdiction      CHAR(2) NOT NULL,
    scope             TEXT NOT NULL DEFAULT 'regional',
    modality          TEXT NOT NULL,
    feature_id        TEXT NOT NULL,
    feature_value     DOUBLE PRECISION,
    feature_z         DOUBLE PRECISION,
    feature_transform TEXT,
    source_table      TEXT,
    source_detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    as_of_policy      TEXT NOT NULL,
    raw_observation_date DATE,
    available_as_of   DATE,
    stale_months      INTEGER,
    coverage          DOUBLE PRECISION,
    missing_ratio     DOUBLE PRECISION,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, jurisdiction, scope, modality, feature_id)
);

ALTER TABLE fact_cycle_feature_monthly
    ADD COLUMN IF NOT EXISTS raw_observation_date DATE,
    ADD COLUMN IF NOT EXISTS available_as_of DATE,
    ADD COLUMN IF NOT EXISTS stale_months INTEGER;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fact_cycle_feature_monthly_pkey'
          AND conrelid = 'fact_cycle_feature_monthly'::regclass
    ) THEN
        ALTER TABLE fact_cycle_feature_monthly
            DROP CONSTRAINT fact_cycle_feature_monthly_pkey;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fact_cycle_feature_monthly_pkey'
          AND conrelid = 'fact_cycle_feature_monthly'::regclass
    ) THEN
        ALTER TABLE fact_cycle_feature_monthly
            ADD CONSTRAINT fact_cycle_feature_monthly_pkey
            PRIMARY KEY (date, jurisdiction, scope, modality, feature_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cycle_feature_juris_date
    ON fact_cycle_feature_monthly (jurisdiction, date DESC);

CREATE INDEX IF NOT EXISTS idx_cycle_feature_modality
    ON fact_cycle_feature_monthly (jurisdiction, scope, modality, feature_id, date DESC);

COMMENT ON TABLE fact_cycle_feature_monthly IS
    'Monthly point-in-time cycle feature store for macro, market, and fundamental breadth features.';

CREATE TABLE IF NOT EXISTS fact_cycle_model_run (
    run_id              TEXT PRIMARY KEY,
    jurisdiction        CHAR(2) NOT NULL,
    model_family        TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    trained_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    train_start         DATE,
    train_end           DATE,
    feature_set_version TEXT,
    hyperparams_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_path       TEXT,
    status              TEXT NOT NULL DEFAULT 'complete',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cycle_model_run_lookup
    ON fact_cycle_model_run (jurisdiction, model_family, trained_at DESC);

COMMENT ON TABLE fact_cycle_model_run IS
    'Lean run registry for jurisdiction-local cycle models; diagnostics and transition matrices live in JSON.';

CREATE TABLE IF NOT EXISTS fact_cycle_state_monthly (
    run_id                  TEXT NOT NULL REFERENCES fact_cycle_model_run (run_id) ON DELETE CASCADE,
    date                    DATE NOT NULL,
    jurisdiction            CHAR(2) NOT NULL,
    latent_cycle            DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_growth           DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_inflation        DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_rates_liquidity  DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_credit_stress    DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_market           DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    latent_fundamentals     DOUBLE PRECISION[] NOT NULL DEFAULT ARRAY[]::DOUBLE PRECISION[],
    phase_label             TEXT,
    phase_probabilities     JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence              DOUBLE PRECISION,
    uncertainty             DOUBLE PRECISION,
    diagnostics_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    modality_contrib_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, date, jurisdiction)
);

CREATE INDEX IF NOT EXISTS idx_cycle_state_latest
    ON fact_cycle_state_monthly (jurisdiction, date DESC);

CREATE INDEX IF NOT EXISTS idx_cycle_state_run
    ON fact_cycle_state_monthly (run_id, date DESC);

COMMENT ON TABLE fact_cycle_state_monthly IS
    'Monthly learned cycle states, probabilities, uncertainty, and compact diagnostics.';

CREATE TABLE IF NOT EXISTS fact_equity_factor_ic_regime (
    run_id                TEXT NOT NULL REFERENCES fact_cycle_model_run (run_id) ON DELETE CASCADE,
    date                  DATE NOT NULL,
    jurisdiction          CHAR(2) NOT NULL,
    regime_source         TEXT NOT NULL,
    regime_label          TEXT NOT NULL,
    metric_id             TEXT NOT NULL,
    forward_return_window TEXT NOT NULL,
    spearman_ic           DOUBLE PRECISION,
    p_value               DOUBLE PRECISION,
    n_obs                 INTEGER,
    diagnostics_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, date, jurisdiction, regime_source, regime_label, metric_id, forward_return_window)
);

CREATE INDEX IF NOT EXISTS idx_equity_factor_ic_lookup
    ON fact_equity_factor_ic_regime (jurisdiction, regime_source, regime_label, metric_id, date DESC);

COMMENT ON TABLE fact_equity_factor_ic_regime IS
    'Jurisdiction-local regime-conditioned Spearman ICs for equity factors and forward returns.';
