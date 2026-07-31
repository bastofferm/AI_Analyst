-- Compact concept-mapping review queue.
--
-- This table is generated staging evidence for Codex/human review. It is
-- intentionally separate from both the protected production mapping table
-- and the noisy raw retrieval suggestions.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS map_concept_to_taxonomy_review_queue (
    queue_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    normalized_concept_id TEXT NOT NULL,
    mapping_sector TEXT NOT NULL DEFAULT '',
    gics_scope TEXT NOT NULL DEFAULT 'generic'
        CHECK (gics_scope IN ('generic', 'gics_conflict')),
    gics_sector TEXT,
    gics_industry_group TEXT,
    local_name TEXT,
    label_en TEXT,
    label_ja TEXT,
    description TEXT,
    source_concept_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    namespaces TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    fiscal_year_min SMALLINT,
    fiscal_year_max SMALLINT,
    statement_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    taxonomies TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    accounting_standards TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    units TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    root_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    parent_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    concept_paths TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    fact_count BIGINT NOT NULL DEFAULT 0,
    filing_count BIGINT NOT NULL DEFAULT 0,
    reporter_count INTEGER NOT NULL DEFAULT 0,
    first_period_end DATE,
    last_period_end DATE,
    sample_entities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    sample_filings TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    review_class TEXT NOT NULL
        CHECK (review_class IN (
            'core_fundamental',
            'supplemental_numeric',
            'nonfundamental_disclosure',
            'rollforward_component',
            'rate_or_ratio',
            'table_member_noise',
            'unmappable_noise'
    )),
    suggested_target_variable TEXT,
    top_candidate_label TEXT,
    top_candidate_description TEXT,
    top_candidate_category TEXT,
    top_candidate_unit_type TEXT,
    suggested_tier INTEGER,
    suggested_multiplier NUMERIC NOT NULL DEFAULT 1,
    confidence NUMERIC CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    review_status TEXT NOT NULL DEFAULT 'queued',
    decision TEXT NOT NULL DEFAULT 'NEEDS_CODEX_REVIEW',
    reasoning TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_targets JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_version TEXT,
    model_name TEXT,
    mapping_source TEXT NOT NULL DEFAULT 'deterministic_review_queue_builder',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by TEXT
);

COMMENT ON TABLE map_concept_to_taxonomy_review_queue IS
    'Generated compact review queue for Codex/human concept mapping. This is staging evidence only and must not overwrite protected production mappings.';

CREATE INDEX IF NOT EXISTS idx_mctrq_status
    ON map_concept_to_taxonomy_review_queue (jurisdiction, review_status, review_class);

CREATE INDEX IF NOT EXISTS idx_mctrq_fact_count
    ON map_concept_to_taxonomy_review_queue (jurisdiction, fact_count DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mctrq_generated_scope
    ON map_concept_to_taxonomy_review_queue (
        jurisdiction,
        normalized_concept_id,
        mapping_sector,
        gics_scope,
        COALESCE(gics_sector, ''),
        COALESCE(gics_industry_group, ''),
        COALESCE(mapping_source, ''),
        COALESCE(prompt_version, '')
    );
