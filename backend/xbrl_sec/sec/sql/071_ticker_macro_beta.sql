-- Ticker-level macro factor betas.
--
-- Mirrors sec.mv_sector_macro_beta but at the individual-ticker level. Same
-- four factors (growth / inflation / policy / usd) regressed against monthly
-- compound returns from fact_prices_us / fact_prices_jp.
--
-- The table is populated by xbrl_sec.sec.sources.ticker_macro_beta (compute
-- job) — declared here so /api/macro/ticker-exposure has a defined target
-- even before the first compute run.
--
-- Idempotent — safe to re-run.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS mv_ticker_macro_beta (
    date          DATE        NOT NULL,
    jurisdiction  CHAR(2)     NOT NULL,
    ticker        TEXT        NOT NULL,
    factor        TEXT        NOT NULL,   -- 'growth' | 'inflation' | 'policy' | 'usd'
    beta          DOUBLE PRECISION,
    t_stat        DOUBLE PRECISION,
    r2            DOUBLE PRECISION,
    window_n      INTEGER,
    PRIMARY KEY (date, jurisdiction, ticker, factor)
);

CREATE INDEX IF NOT EXISTS idx_mv_ticker_macro_latest
  ON mv_ticker_macro_beta (jurisdiction, ticker, date DESC);

CREATE INDEX IF NOT EXISTS idx_mv_ticker_macro_factor
  ON mv_ticker_macro_beta (jurisdiction, ticker, factor, date DESC);

COMMENT ON TABLE mv_ticker_macro_beta IS
    'Rolling 24-month OLS beta of each ticker''s monthly return vs four macro factors. Populated by ticker_macro_beta compute job.';
