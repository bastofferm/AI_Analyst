SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS market_source_item_state (
    source        TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    run_id        UUID REFERENCES pipeline_stage_run(run_id),
    status        TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','skipped')),
    source_url    TEXT,
    source_hash   TEXT,
    min_date      DATE,
    max_date      DATE,
    rows_in       BIGINT NOT NULL DEFAULT 0,
    rows_out      BIGINT NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_key)
);

CREATE INDEX IF NOT EXISTS idx_market_source_item_state_run
    ON market_source_item_state (run_id);
CREATE INDEX IF NOT EXISTS idx_market_source_item_state_status
    ON market_source_item_state (source, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS stage_prices (
    run_id       UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    source       TEXT NOT NULL CHECK (source IN ('prices_us','prices_jp')),
    source_key   TEXT NOT NULL,
    date         DATE NOT NULL,
    ticker       TEXT NOT NULL,
    close        DOUBLE PRECISION,
    adj_close    DOUBLE PRECISION,
    return       DOUBLE PRECISION,
    log_return   DOUBLE PRECISION,
    abs_diff     DOUBLE PRECISION,
    volume       BIGINT,
    currency     TEXT,
    jurisdiction TEXT NOT NULL,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source, ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_stage_prices_source_key
    ON stage_prices (source, source_key);

CREATE TABLE IF NOT EXISTS stage_macro (
    run_id     UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    source     TEXT NOT NULL DEFAULT 'macro',
    source_key TEXT NOT NULL,
    series_id  TEXT NOT NULL,
    date       DATE NOT NULL,
    value      DOUBLE PRECISION,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, series_id, date)
);

CREATE INDEX IF NOT EXISTS idx_stage_macro_source_key
    ON stage_macro (source_key);

CREATE TABLE IF NOT EXISTS stage_cross_asset (
    run_id      UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'cross_asset',
    source_key  TEXT NOT NULL,
    date        DATE NOT NULL,
    ticker      TEXT NOT NULL,
    asset_class TEXT,
    close       DOUBLE PRECISION,
    adj_close   DOUBLE PRECISION,
    return      DOUBLE PRECISION,
    log_return  DOUBLE PRECISION,
    volume      BIGINT,
    currency    TEXT,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_stage_cross_asset_source_key
    ON stage_cross_asset (source_key);

CREATE TABLE IF NOT EXISTS stage_fama_french (
    run_id     UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    source     TEXT NOT NULL DEFAULT 'fama_french',
    source_key TEXT NOT NULL,
    date       DATE NOT NULL,
    factor     TEXT NOT NULL,
    value      DOUBLE PRECISION,
    dataset    TEXT NOT NULL,
    return_pct DOUBLE PRECISION,
    return_log DOUBLE PRECISION,
    level      DOUBLE PRECISION,
    loaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stage_fama_french_source_key
    ON stage_fama_french (source_key);
CREATE INDEX IF NOT EXISTS idx_stage_fama_french_run_dataset
    ON stage_fama_french (run_id, dataset);

COMMENT ON TABLE market_source_item_state IS
    'Item-level control table for market data downloads: tickers, FRED series, cross-asset tickers, and Ken French datasets.';
COMMENT ON TABLE stage_prices IS
    'Run-scoped staging rows for US/JP equity price downloads before merging into fact_prices_us/fact_prices_jp.';
COMMENT ON TABLE stage_macro IS
    'Run-scoped FRED macro staging rows before merging into fact_macro.';
COMMENT ON TABLE stage_cross_asset IS
    'Run-scoped cross-asset price staging rows before merging into fact_cross_asset.';
COMMENT ON TABLE stage_fama_french IS
    'Run-scoped Ken French staging rows before merging into fact_fama_french.';
