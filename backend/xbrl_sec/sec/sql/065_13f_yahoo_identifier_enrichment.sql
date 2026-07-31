SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_13f_yahoo_identifier_enrichment (
    cusip TEXT NOT NULL CHECK (cusip ~ '^[A-Z0-9]{9}$'),
    yahoo_symbol TEXT NOT NULL DEFAULT '',
    search_query TEXT,
    query_strategy TEXT,
    query_rank INTEGER,
    issuer_name TEXT,
    security_title TEXT,
    asset_bucket TEXT,
    discovered_ticker TEXT,
    discovered_isin TEXT,
    yahoo_short_name TEXT,
    yahoo_long_name TEXT,
    yahoo_exchange TEXT,
    yahoo_quote_type TEXT,
    sector TEXT,
    industry_group TEXT,
    confidence_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL
        CHECK (status IN ('accepted', 'ticker_only', 'conflict', 'not_found', 'error')),
    status_reason TEXT,
    applied BOOLEAN NOT NULL DEFAULT false,
    applied_at TIMESTAMPTZ,
    error_type TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cusip, yahoo_symbol)
);

ALTER TABLE fact_13f_yahoo_identifier_enrichment
    DROP COLUMN IF EXISTS raw_yahoo_payload,
    DROP COLUMN IF EXISTS raw_yfinance_payload;

CREATE INDEX IF NOT EXISTS idx_fact_13f_yahoo_identifier_enrichment_status
    ON fact_13f_yahoo_identifier_enrichment (status, confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_yahoo_identifier_enrichment_apply
    ON fact_13f_yahoo_identifier_enrichment (applied, status, updated_at DESC);
