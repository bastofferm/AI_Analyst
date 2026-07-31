SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_security_identifier_us (
    cusip TEXT PRIMARY KEY CHECK (cusip ~ '^[A-Z0-9]{9}$'),
    cusip8 TEXT NOT NULL,
    cusip6 TEXT NOT NULL,
    issuer_cik TEXT REFERENCES dim_company_us(cik),
    issuer_ticker TEXT,
    issuer_name TEXT,
    security_title TEXT,
    security_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK (security_type IN ('common_equity', 'preferred', 'adr', 'etf_or_fund', 'debt', 'option_or_derivative', 'unknown')),
    resolution_status TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (resolution_status IN ('resolved', 'ambiguous', 'non_company_security', 'unresolved')),
    confidence_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    source_priority INTEGER,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dim_security_identifier_us_cusip8
    ON dim_security_identifier_us (cusip8);

CREATE INDEX IF NOT EXISTS idx_dim_security_identifier_us_issuer
    ON dim_security_identifier_us (issuer_cik, issuer_ticker);

CREATE INDEX IF NOT EXISTS idx_dim_security_identifier_us_status
    ON dim_security_identifier_us (resolution_status, security_type);

CREATE TABLE IF NOT EXISTS fact_security_identifier_evidence_us (
    evidence_id BIGSERIAL PRIMARY KEY,
    cusip TEXT,
    cusip8 TEXT,
    cusip6 TEXT,
    candidate_cik TEXT,
    candidate_ticker TEXT,
    candidate_name TEXT,
    observed_issuer_name TEXT,
    observed_security_title TEXT,
    source_name TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    confidence_score NUMERIC(5,2) NOT NULL,
    row_count BIGINT NOT NULL DEFAULT 0,
    value_observed NUMERIC,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fact_security_identifier_evidence_us_cusip
    ON fact_security_identifier_evidence_us (cusip, source_priority, confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_fact_security_identifier_evidence_us_candidate
    ON fact_security_identifier_evidence_us (candidate_cik, candidate_ticker);

CREATE INDEX IF NOT EXISTS idx_fact_security_identifier_evidence_us_source
    ON fact_security_identifier_evidence_us (source_name, source_key);
