-- Metrics and recon layer for the MZQA xbrl_sec.sec pipeline.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_metric_definitions (
    metric_id TEXT PRIMARY KEY,
    category TEXT,
    name TEXT,
    importance INTEGER,
    formula TEXT,
    required_line_items TEXT[],
    note TEXT,
    unit_type TEXT,
    metric_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ref_entity_ticker (
    entity_id TEXT NOT NULL,
    entity_id_type TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    ticker TEXT NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, entity_id_type, ticker)
);

CREATE TABLE IF NOT EXISTS fact_prices_us (
    date DATE NOT NULL,
    ticker TEXT NOT NULL,
    close DOUBLE PRECISION,
    return DOUBLE PRECISION,
    log_return DOUBLE PRECISION,
    abs_diff DOUBLE PRECISION,
    currency TEXT,
    jurisdiction TEXT NOT NULL DEFAULT 'US',
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fact_prices_jp (
    date DATE NOT NULL,
    ticker TEXT NOT NULL,
    close DOUBLE PRECISION,
    return DOUBLE PRECISION,
    log_return DOUBLE PRECISION,
    abs_diff DOUBLE PRECISION,
    currency TEXT,
    jurisdiction TEXT NOT NULL DEFAULT 'JP',
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fact_metrics_us (
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    primary_id TEXT,
    primary_id_type TEXT,
    jurisdiction TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    metric_id TEXT NOT NULL,
    formula TEXT,
    metric_type TEXT,
    category TEXT,
    importance INTEGER,
    unit_type TEXT,
    value DOUBLE PRECISION,
    currency TEXT,
    fallback_applied BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, cik, fiscal_year, fiscal_period, metric_id)
);

CREATE TABLE IF NOT EXISTS fact_metrics_jp (
    ticker TEXT NOT NULL,
    edinet_code TEXT NOT NULL,
    primary_id TEXT,
    primary_id_type TEXT,
    jurisdiction TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    metric_id TEXT NOT NULL,
    formula TEXT,
    metric_type TEXT,
    category TEXT,
    importance INTEGER,
    unit_type TEXT,
    value DOUBLE PRECISION,
    currency TEXT,
    fallback_applied BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, edinet_code, fiscal_year, fiscal_period, metric_id)
);

CREATE TABLE IF NOT EXISTS fact_metrics_recon_us (
    ticker TEXT NOT NULL,
    cik TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    metric_id TEXT NOT NULL,
    formula TEXT,
    formula_with_values TEXT,
    value DOUBLE PRECISION,
    currency TEXT,
    computed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, cik, fiscal_year, fiscal_period, metric_id)
);

CREATE TABLE IF NOT EXISTS fact_metrics_recon_jp (
    ticker TEXT NOT NULL,
    edinet_code TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    metric_id TEXT NOT NULL,
    formula TEXT,
    formula_with_values TEXT,
    value DOUBLE PRECISION,
    currency TEXT,
    computed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, edinet_code, fiscal_year, fiscal_period, metric_id)
);

CREATE INDEX IF NOT EXISTS idx_metrics_us_metric ON fact_metrics_us (metric_id);
CREATE INDEX IF NOT EXISTS idx_metrics_jp_metric ON fact_metrics_jp (metric_id);
CREATE INDEX IF NOT EXISTS idx_metrics_us_period ON fact_metrics_us (cik, fiscal_year, fiscal_period);
CREATE INDEX IF NOT EXISTS idx_metrics_jp_period ON fact_metrics_jp (edinet_code, fiscal_year, fiscal_period);
