SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_13f_openfigi_identifier_enrichment (
    cusip TEXT NOT NULL CHECK (cusip ~ '^[A-Z0-9]{9}$'),
    issuer_name TEXT,
    security_title TEXT,
    asset_bucket TEXT,
    openfigi_ticker TEXT,
    openfigi_name TEXT,
    openfigi_exch_code TEXT,
    openfigi_security_type TEXT,
    openfigi_security_type2 TEXT,
    openfigi_market_sector TEXT,
    openfigi_figi TEXT,
    openfigi_share_class_figi TEXT,
    openfigi_composite_figi TEXT,
    openfigi_listings_returned INTEGER,
    confidence_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL
        CHECK (status IN ('accepted', 'multi_listing', 'not_found', 'error')),
    status_reason TEXT,
    applied BOOLEAN NOT NULL DEFAULT false,
    applied_at TIMESTAMPTZ,
    error_type TEXT,
    error_message TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cusip)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_openfigi_identifier_enrichment_status
    ON fact_13f_openfigi_identifier_enrichment (status, confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_openfigi_identifier_enrichment_apply
    ON fact_13f_openfigi_identifier_enrichment (applied, status, updated_at DESC);
