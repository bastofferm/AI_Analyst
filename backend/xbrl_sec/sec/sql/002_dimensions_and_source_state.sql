-- Dimension and reference support for the first raw pipeline migration slice.
-- This migration is intentionally additive and targets only xbrl_sec.sec.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_company_us (
    cik TEXT PRIMARY KEY,
    name TEXT,
    primary_ticker TEXT,
    exchange TEXT,
    entity_type TEXT,
    entity_class TEXT,
    sic TEXT,
    sic_description TEXT,
    fiscal_year_end TEXT,
    country_code TEXT,
    state_of_incorporation TEXT,
    isin TEXT,
    lei TEXT,
    gics_sector_code TEXT,
    gics_sector_name TEXT,
    gics_industry_group_code TEXT,
    gics_industry_group_name TEXT,
    mapping_sector TEXT,
    source TEXT NOT NULL DEFAULT 'SEC',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_company_jp (
    edinet_code TEXT PRIMARY KEY,
    name TEXT,
    primary_ticker TEXT,
    sec_code TEXT,
    jcn TEXT,
    country_code TEXT NOT NULL DEFAULT 'JP',
    fiscal_year_end TEXT,
    isin TEXT,
    lei TEXT,
    gics_sector_code TEXT,
    gics_sector_name TEXT,
    gics_industry_group_code TEXT,
    gics_industry_group_name TEXT,
    mapping_sector TEXT,
    is_active BOOLEAN,
    source TEXT NOT NULL DEFAULT 'EDINET',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_filing_entity_stage ON source_filing_state (jurisdiction, entity_id, parsed);
CREATE INDEX IF NOT EXISTS idx_source_filing_hash ON source_filing_state (jurisdiction, source_hash);

ALTER TABLE source_filing_state ADD COLUMN IF NOT EXISTS source_path TEXT;
ALTER TABLE source_filing_state ADD COLUMN IF NOT EXISTS raw_payload JSONB;
