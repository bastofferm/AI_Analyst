-- Concept universe evidence layer.
--
-- These concept-universe tables are generated evidence tables and may be
-- rebuilt from parsed raw facts. Curated production mappings remain protected
-- in map_concept_to_taxonomy_versioned.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_concept_universe_observation (
    observation_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    concept_id TEXT NOT NULL,
    namespace TEXT,
    local_name TEXT,
    fiscal_year SMALLINT,
    taxonomy TEXT,
    accounting_standard TEXT,
    mapping_sector TEXT NOT NULL DEFAULT '',
    gics_sector_code TEXT,
    gics_sector_name TEXT,
    gics_industry_group_code TEXT,
    gics_industry_group_name TEXT,
    statement_type TEXT,
    root_id TEXT,
    parent_id TEXT,
    concept_path TEXT,
    concept_id_level SMALLINT,
    unit TEXT,
    value_type TEXT,
    label_en TEXT,
    label_ja TEXT,
    description TEXT,
    reporter_count INTEGER NOT NULL DEFAULT 0,
    filing_count INTEGER NOT NULL DEFAULT 0,
    fact_count BIGINT NOT NULL DEFAULT 0,
    first_period_end DATE,
    last_period_end DATE,
    first_filed_date DATE,
    last_filed_date DATE,
    sample_entities TEXT[],
    sample_filings TEXT[],
    sample_units TEXT[],
    sample_concept_paths TEXT[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_concept_universe_observation IS
    'Generated concept evidence by jurisdiction/year/sector/GICS/statement context. Safe to rebuild from parsed facts.';

CREATE INDEX IF NOT EXISTS idx_rcuo_concept_year
    ON ref_concept_universe_observation (jurisdiction, concept_id, fiscal_year);

CREATE INDEX IF NOT EXISTS idx_rcuo_sector
    ON ref_concept_universe_observation (jurisdiction, mapping_sector);

CREATE INDEX IF NOT EXISTS idx_rcuo_gics
    ON ref_concept_universe_observation (jurisdiction, gics_sector_code, gics_industry_group_code);

CREATE INDEX IF NOT EXISTS idx_rcuo_statement
    ON ref_concept_universe_observation (jurisdiction, statement_type);

CREATE TABLE IF NOT EXISTS ref_concept_universe_corp (
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    concept_id TEXT NOT NULL,
    namespace TEXT,
    local_name TEXT,
    mapping_sector TEXT NOT NULL DEFAULT 'corp',
    label_en TEXT,
    label_ja TEXT,
    description TEXT,
    first_seen_year SMALLINT,
    last_seen_year SMALLINT,
    reporter_count INTEGER NOT NULL DEFAULT 0,
    filing_count INTEGER NOT NULL DEFAULT 0,
    fact_count BIGINT NOT NULL DEFAULT 0,
    statement_types TEXT[],
    taxonomies TEXT[],
    units TEXT[],
    gics_sector_codes TEXT[],
    gics_industry_group_codes TEXT[],
    sample_entities TEXT[],
    sample_filings TEXT[],
    sample_concept_paths TEXT[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, concept_id, mapping_sector)
);

CREATE TABLE IF NOT EXISTS ref_concept_universe_bank_financial
    (LIKE ref_concept_universe_corp INCLUDING ALL);

CREATE TABLE IF NOT EXISTS ref_concept_universe_non_bank_financial
    (LIKE ref_concept_universe_corp INCLUDING ALL);

COMMENT ON TABLE ref_concept_universe_corp IS
    'Generated rollup from ref_concept_universe_observation for mapping_sector=corp.';
COMMENT ON TABLE ref_concept_universe_bank_financial IS
    'Generated rollup from ref_concept_universe_observation for mapping_sector=bank_financial.';
COMMENT ON TABLE ref_concept_universe_non_bank_financial IS
    'Generated rollup from ref_concept_universe_observation for mapping_sector=non_bank_financial.';
