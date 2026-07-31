-- 123_etf_price_fallback_sources.sql
-- Price fallback provenance and justETF metadata audit layer.

ALTER TABLE sec.fact_prices_etf
    ADD COLUMN IF NOT EXISTS history_kind VARCHAR(40) NOT NULL DEFAULT 'market_price',
    ADD COLUMN IF NOT EXISTS source_symbol TEXT;

CREATE INDEX IF NOT EXISTS idx_fact_prices_etf_source_kind
    ON sec.fact_prices_etf(source, history_kind);

ALTER TABLE sec.dim_etf
    ADD COLUMN IF NOT EXISTS wkn TEXT,
    ADD COLUMN IF NOT EXISTS distribution_policy TEXT,
    ADD COLUMN IF NOT EXISTS fund_domicile TEXT,
    ADD COLUMN IF NOT EXISTS metadata_sources JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE sec.dim_etf_profile
    ADD COLUMN IF NOT EXISTS factsheet_url TEXT,
    ADD COLUMN IF NOT EXISTS kid_url TEXT;

CREATE TABLE IF NOT EXISTS sec.etf_price_source_candidate (
    isin             VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    source           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    mic              VARCHAR(6) NOT NULL,
    history_kind     VARCHAR(40) NOT NULL DEFAULT 'market_price',
    status           VARCHAR(30) NOT NULL DEFAULT 'empty',
    first_price_date DATE,
    last_price_date  DATE,
    price_rows       INT NOT NULL DEFAULT 0,
    currency         VARCHAR(3),
    error_message    TEXT,
    source_url       TEXT,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at     TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, source, symbol, history_kind)
);

CREATE INDEX IF NOT EXISTS idx_etf_price_source_candidate_status
    ON sec.etf_price_source_candidate(status, source, last_price_date DESC);

CREATE INDEX IF NOT EXISTS idx_etf_price_source_candidate_isin
    ON sec.etf_price_source_candidate(isin, first_price_date, price_rows DESC);

CREATE TABLE IF NOT EXISTS sec.etf_justetf_profile (
    isin                   VARCHAR(12) PRIMARY KEY REFERENCES sec.dim_etf(isin),
    provider_id            TEXT,
    justetf_found          BOOLEAN NOT NULL DEFAULT FALSE,
    primary_ticker         TEXT,
    wkn                    TEXT,
    xetra_symbol           TEXT,
    ric                    TEXT,
    clean_name             TEXT,
    fund_family            TEXT,
    ter_pct                NUMERIC(8,6),
    aum_eur                NUMERIC(20,2),
    inception_date         DATE,
    distribution_policy    TEXT,
    replication_method     TEXT,
    fund_domicile          TEXT,
    fund_currency          VARCHAR(3),
    factsheet_url          TEXT,
    kid_url                TEXT,
    documents              JSONB NOT NULL DEFAULT '{}'::jsonb,
    history_available_hint BOOLEAN NOT NULL DEFAULT FALSE,
    legal_price_source     TEXT NOT NULL DEFAULT 'metadata_only_unlicensed',
    raw_payload            JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_etf_justetf_profile_found
    ON sec.etf_justetf_profile(justetf_found, provider_id);
