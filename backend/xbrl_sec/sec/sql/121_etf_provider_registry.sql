-- 121_etf_provider_registry.sql
-- Canonical ETF provider registry + official holdings fetch state.
-- Idempotent: apply_schema re-runs every file.

CREATE TABLE IF NOT EXISTS sec.dim_etf_provider (
    provider_id   TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    domain        TEXT,
    aliases       TEXT[] NOT NULL DEFAULT '{}'::TEXT[],
    source_status VARCHAR(30) NOT NULL DEFAULT 'fallback_only',
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE sec.dim_etf ADD COLUMN IF NOT EXISTS provider_id TEXT;

DO $$
BEGIN
    ALTER TABLE sec.dim_etf
        ADD CONSTRAINT fk_dim_etf_provider
        FOREIGN KEY (provider_id) REFERENCES sec.dim_etf_provider(provider_id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_dim_etf_provider_id
    ON sec.dim_etf(provider_id);

CREATE TABLE IF NOT EXISTS sec.etf_holdings_fetch_state (
    isin             VARCHAR(12) PRIMARY KEY REFERENCES sec.dim_etf(isin),
    provider_id      TEXT REFERENCES sec.dim_etf_provider(provider_id),
    source           TEXT,
    status           VARCHAR(30) NOT NULL DEFAULT 'pending',
    row_count        INT,
    source_url       TEXT,
    as_of_date       DATE,
    last_attempt_at  TIMESTAMPTZ,
    last_success_at  TIMESTAMPTZ,
    error_message    TEXT,
    retry_count      INT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_etf_holdings_fetch_state_provider
    ON sec.etf_holdings_fetch_state(provider_id, status);
