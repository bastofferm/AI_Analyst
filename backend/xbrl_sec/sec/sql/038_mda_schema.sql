-- SEC MD&A extraction schema.
-- Narrative text is stored separately from numerical fundamentals.

SET search_path TO sec, public;

ALTER TABLE source_filing_state
    ADD COLUMN IF NOT EXISTS xbrl_html_extracted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS source_mda_state (
    cik TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    form_type TEXT,
    filed_date DATE,
    availability_status TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (availability_status IN ('available','missing_html','missing_zip','not_attempted')),
    html_path TEXT,
    source_package_path TEXT,
    source_html_sha256 TEXT,
    html_size_bytes BIGINT,
    extraction_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    extraction_succeeded BOOLEAN NOT NULL DEFAULT FALSE,
    last_attempted_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_source_mda_state_availability
    ON source_mda_state (availability_status, filed_date DESC);

CREATE INDEX IF NOT EXISTS idx_source_mda_state_cik_availability
    ON source_mda_state (cik, availability_status);

CREATE TABLE IF NOT EXISTS fact_mda_sections_us (
    cik TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    section_id TEXT NOT NULL CHECK (section_id IN ('item_7','item_2','item_7a')),
    form_type TEXT NOT NULL,
    filed_date DATE NOT NULL,
    period_end DATE,
    section_text TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT NOT NULL CHECK (extraction_method IN ('ixbrl_textblock','html_regex')),
    extraction_quality TEXT NOT NULL DEFAULT 'clean' CHECK (extraction_quality IN ('clean','dirty')),
    extraction_error TEXT,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, filing_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_mda_sections_us_cik_filed
    ON fact_mda_sections_us (cik, filed_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_mda_sections_us_cik_section
    ON fact_mda_sections_us (cik, section_id);

CREATE INDEX IF NOT EXISTS idx_fact_mda_sections_us_quality
    ON fact_mda_sections_us (extraction_method, extraction_quality);

CREATE TABLE IF NOT EXISTS source_mda_state_jp (
    edinet_code TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    doc_type_code TEXT,
    filed_date DATE,
    availability_status TEXT NOT NULL DEFAULT 'not_attempted'
        CHECK (availability_status IN ('available','missing_html','missing_zip','not_attempted')),
    html_path TEXT,
    source_package_path TEXT,
    source_html_sha256 TEXT,
    html_size_bytes BIGINT,
    extraction_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    extraction_succeeded BOOLEAN NOT NULL DEFAULT FALSE,
    last_attempted_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edinet_code, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_source_mda_state_jp_availability
    ON source_mda_state_jp (availability_status, filed_date DESC);

CREATE INDEX IF NOT EXISTS idx_source_mda_state_jp_entity_availability
    ON source_mda_state_jp (edinet_code, availability_status);

CREATE TABLE IF NOT EXISTS fact_mda_sections_jp (
    edinet_code TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    section_id TEXT NOT NULL CHECK (section_id IN ('business_status')),
    doc_type_code TEXT NOT NULL,
    filed_date DATE NOT NULL,
    period_end DATE,
    section_text TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    extraction_method TEXT NOT NULL CHECK (extraction_method IN ('edinet_html_file')),
    extraction_quality TEXT NOT NULL DEFAULT 'clean' CHECK (extraction_quality IN ('clean','dirty')),
    extraction_error TEXT,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edinet_code, filing_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_mda_sections_jp_entity_filed
    ON fact_mda_sections_jp (edinet_code, filed_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_mda_sections_jp_quality
    ON fact_mda_sections_jp (extraction_method, extraction_quality);

ALTER TABLE source_mda_state
    ADD COLUMN IF NOT EXISTS source_package_path TEXT,
    ADD COLUMN IF NOT EXISTS source_html_sha256 TEXT;

ALTER TABLE source_mda_state_jp
    ADD COLUMN IF NOT EXISTS source_package_path TEXT,
    ADD COLUMN IF NOT EXISTS source_html_sha256 TEXT;

CREATE OR REPLACE VIEW vw_mda_sections AS
SELECT
    'US'::TEXT AS jurisdiction,
    cik AS entity_id,
    filing_id,
    section_id,
    form_type AS form_or_doc_type,
    filed_date,
    period_end,
    section_text,
    char_count,
    extraction_method,
    extraction_quality,
    extraction_error,
    extracted_at,
    updated_at
FROM fact_mda_sections_us
UNION ALL
SELECT
    'JP'::TEXT AS jurisdiction,
    edinet_code AS entity_id,
    filing_id,
    section_id,
    doc_type_code AS form_or_doc_type,
    filed_date,
    period_end,
    section_text,
    char_count,
    extraction_method,
    extraction_quality,
    extraction_error,
    extracted_at,
    updated_at
FROM fact_mda_sections_jp;
