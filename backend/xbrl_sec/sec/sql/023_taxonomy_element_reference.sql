-- Authoritative taxonomy concept metadata used by the LLM mapping workflow.
-- The table is populated from spec/*_all_years.json and, where available,
-- official XBRL taxonomy ZIP/linkbase packages.

CREATE TABLE IF NOT EXISTS ref_taxonomy_element (
    taxonomy_id SERIAL PRIMARY KEY,
    namespace TEXT NOT NULL,
    local_name TEXT NOT NULL,
    concept_id TEXT GENERATED ALWAYS AS (namespace || '/' || local_name) STORED,
    taxonomy_year INT NOT NULL,
    label TEXT,
    label_terse TEXT,
    label_verbose TEXT,
    documentation TEXT,
    period_type TEXT,
    balance_type TEXT,
    data_type TEXT,
    is_abstract BOOLEAN DEFAULT FALSE,
    is_deprecated BOOLEAN DEFAULT FALSE,
    authoritative_refs JSONB,
    parent_concept TEXT,
    statement_type TEXT,
    sector_scope TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, local_name, taxonomy_year)
);

ALTER TABLE ref_taxonomy_element
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

COMMENT ON TABLE ref_taxonomy_element IS
    'Versioned taxonomy element labels/descriptions used as evidence for concept mapping. This is reference evidence, not the production mapping table.';
COMMENT ON COLUMN ref_taxonomy_element.documentation IS
    'Authoritative concept description/documentation when available. Loaded from *_all_years specs and XBRL linkbases.';

CREATE INDEX IF NOT EXISTS idx_ref_taxonomy_element_concept_year
    ON ref_taxonomy_element (concept_id, taxonomy_year DESC);

CREATE INDEX IF NOT EXISTS idx_ref_taxonomy_element_namespace_year
    ON ref_taxonomy_element (namespace, taxonomy_year DESC);
