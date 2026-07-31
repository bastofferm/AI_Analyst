SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_market_metrics (
    jurisdiction  TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    fiscal_year   SMALLINT NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end    DATE,
    market_date   DATE,
    metric_id     TEXT NOT NULL,
    value         NUMERIC,
    currency      TEXT,
    source        TEXT NOT NULL DEFAULT 'yfinance',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, entity_id, ticker, fiscal_year, fiscal_period, metric_id)
);

COMMENT ON TABLE fact_market_metrics IS
    'Market-derived entity metrics such as stock price, market capitalization, and beta. Kept separate from standardized XBRL fundamentals.';

CREATE INDEX IF NOT EXISTS idx_fact_market_metrics_entity_period
    ON fact_market_metrics (jurisdiction, entity_id, fiscal_year DESC, fiscal_period);

CREATE INDEX IF NOT EXISTS idx_fact_market_metrics_metric_date
    ON fact_market_metrics (metric_id, market_date DESC);
