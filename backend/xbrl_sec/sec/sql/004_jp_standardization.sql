-- JP standardization layer for the MZQA xbrl_sec.sec pipeline.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS map_concept_to_taxonomy (
    concept_id TEXT NOT NULL,
    target_variable TEXT NOT NULL,
    tier INTEGER NOT NULL,
    multiplier NUMERIC NOT NULL DEFAULT 1,
    reasoning TEXT,
    mapping_sector TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (concept_id, mapping_sector)
);

CREATE TABLE IF NOT EXISTS ref_standardized_line_items (
    line_item_id TEXT PRIMARY KEY,
    category TEXT,
    label TEXT,
    description TEXT,
    is_filed BOOLEAN,
    importance INTEGER,
    formula TEXT,
    mapping_sector TEXT,
    unit_type TEXT,
    std_concept_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_reference_mapping_duplicates (
    concept_id TEXT NOT NULL,
    mapping_sector TEXT NOT NULL DEFAULT '',
    duplicate_count INTEGER NOT NULL,
    distinct_target_variables INTEGER NOT NULL,
    distinct_tiers INTEGER NOT NULL,
    sample_target_variables TEXT[] NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (concept_id, mapping_sector)
);

CREATE TABLE IF NOT EXISTS fact_fundamentals_std_jp (
    edinet_code TEXT NOT NULL,
    jurisdiction TEXT NOT NULL DEFAULT 'JP',
    fiscal_year SMALLINT NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    line_item_id TEXT NOT NULL,
    metric_type VARCHAR(16) NOT NULL DEFAULT 'RAW',
    value NUMERIC,
    currency TEXT,
    source_concept_id TEXT,
    filing_form TEXT,
    filed_date DATE,
    filing_id TEXT,
    context_id TEXT,
    dimension_signature TEXT,
    concept_path TEXT,
    std_concept_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edinet_code, jurisdiction, fiscal_year, fiscal_period, line_item_id)
);

CREATE INDEX IF NOT EXISTS idx_ffstd_jp_line_item
    ON fact_fundamentals_std_jp (line_item_id);

CREATE INDEX IF NOT EXISTS idx_ffstd_jp_period
    ON fact_fundamentals_std_jp (edinet_code, fiscal_year, fiscal_period);
