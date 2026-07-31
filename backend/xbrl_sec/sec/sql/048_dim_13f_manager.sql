-- Comprehensive 13F manager dimensional table.
--
-- Idempotent hardening for the canonical manager metadata table. This file
-- must never drop data; existing databases are migrated by 049.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_13f_manager (
    manager_cik TEXT PRIMARY KEY,
    manager_name TEXT NOT NULL,
    manager_type TEXT NOT NULL DEFAULT 'unknown',
    is_public_company BOOLEAN NOT NULL DEFAULT false,
    public_entity_cik TEXT,
    name_source TEXT,
    crd_number TEXT,
    sec_file_number TEXT,
    form_13f_file_number TEXT,
    report_type TEXT,
    street1 TEXT,
    street2 TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    filing_count_primary INTEGER NOT NULL DEFAULT 0,
    filing_count_other INTEGER NOT NULL DEFAULT 0,
    filing_count_total INTEGER NOT NULL DEFAULT 0,
    first_quarter_filed DATE,
    last_quarter_filed DATE,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS manager_type TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS is_public_company BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS public_entity_cik TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS name_source TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS crd_number TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS sec_file_number TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS form_13f_file_number TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS report_type TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS street1 TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS street2 TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS city TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS zip_code TEXT;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS filing_count_primary INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS filing_count_other INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS filing_count_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS first_quarter_filed DATE;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS last_quarter_filed DATE;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE dim_13f_manager ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_dim_13f_manager_name
    ON dim_13f_manager (manager_name);

CREATE INDEX IF NOT EXISTS idx_dim_13f_manager_name_source
    ON dim_13f_manager (name_source);
