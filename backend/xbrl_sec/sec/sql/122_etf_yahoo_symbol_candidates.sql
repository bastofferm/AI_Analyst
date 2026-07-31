-- 122_etf_yahoo_symbol_candidates.sql
-- Staging + evidence table for resolving ETF ISINs to concrete Yahoo Finance
-- quote symbols. Resolver runs write here first; only high-confidence rows are
-- promoted into sec.dim_etf_profile / sec.dim_etf_listing.

CREATE TABLE IF NOT EXISTS sec.etf_yahoo_symbol_candidate (
    isin               VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    query_strategy     TEXT NOT NULL,
    query_text         TEXT NOT NULL,
    candidate_symbol   TEXT NOT NULL,
    candidate_name     TEXT,
    candidate_exchange TEXT,
    candidate_currency VARCHAR(10),
    quote_type         TEXT,
    source_url         TEXT,
    rank               INT,
    score              NUMERIC(6,2) NOT NULL DEFAULT 0,
    status             VARCHAR(24) NOT NULL DEFAULT 'staged',
    status_reason      TEXT,
    price_validated    BOOLEAN NOT NULL DEFAULT FALSE,
    price_rows         INT NOT NULL DEFAULT 0,
    last_price_date    DATE,
    evidence           JSONB NOT NULL DEFAULT '{}'::jsonb,
    searched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    validated_at       TIMESTAMPTZ,
    promoted_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, candidate_symbol, query_strategy, query_text)
);

CREATE INDEX IF NOT EXISTS idx_etf_yahoo_symbol_candidate_status_score
    ON sec.etf_yahoo_symbol_candidate(status, score DESC);

CREATE INDEX IF NOT EXISTS idx_etf_yahoo_symbol_candidate_isin_score
    ON sec.etf_yahoo_symbol_candidate(isin, score DESC);

CREATE INDEX IF NOT EXISTS idx_etf_yahoo_symbol_candidate_symbol
    ON sec.etf_yahoo_symbol_candidate(candidate_symbol);
