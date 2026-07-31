SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_13f_prices_yahoo (
    date DATE NOT NULL,
    cusip TEXT NOT NULL CHECK (cusip ~ '^[A-Z0-9]{9}$'),
    ticker TEXT NOT NULL,
    close DOUBLE PRECISION,
    return DOUBLE PRECISION,
    log_return DOUBLE PRECISION,
    abs_diff DOUBLE PRECISION,
    currency TEXT,
    jurisdiction TEXT NOT NULL DEFAULT 'US',
    adj_close DOUBLE PRECISION,
    volume BIGINT,
    shares_outstanding BIGINT,
    identifier_status TEXT,
    source_name TEXT NOT NULL DEFAULT 'yahoo_finance',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cusip, ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_prices_yahoo_ticker_date
    ON fact_13f_prices_yahoo (ticker, date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_prices_yahoo_cusip_date
    ON fact_13f_prices_yahoo (cusip, date DESC);

COMMENT ON TABLE fact_13f_prices_yahoo IS
    'Daily Yahoo/yfinance OHLCV-derived price facts for 13F securities identified by CUSIP and Yahoo ticker.';
