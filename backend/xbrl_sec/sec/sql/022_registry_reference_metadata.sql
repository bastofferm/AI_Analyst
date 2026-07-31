-- Registry metadata for standardized line items and metrics.
--
-- The registry lives in spec/line_item_metric_registry.json. These columns make
-- the existing lean reference tables expressive enough to hold that source
-- without adding parallel tables.

SET search_path TO sec, public;

ALTER TABLE ref_standardized_line_items
    ADD COLUMN IF NOT EXISTS statement_type TEXT,
    ADD COLUMN IF NOT EXISTS sector_scope TEXT NOT NULL DEFAULT 'universal',
    ADD COLUMN IF NOT EXISTS gics_sector TEXT,
    ADD COLUMN IF NOT EXISTS maps_into_metrics TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS registry_version TEXT,
    ADD COLUMN IF NOT EXISTS registry_source TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE ref_metric_definitions
    ADD COLUMN IF NOT EXISTS formula_symbolic TEXT,
    ADD COLUMN IF NOT EXISTS formula_sql TEXT,
    ADD COLUMN IF NOT EXISTS sector_scope TEXT NOT NULL DEFAULT 'universal',
    ADD COLUMN IF NOT EXISTS gics_sector TEXT,
    ADD COLUMN IF NOT EXISTS interpretation TEXT,
    ADD COLUMN IF NOT EXISTS academic_source TEXT,
    ADD COLUMN IF NOT EXISTS registry_version TEXT,
    ADD COLUMN IF NOT EXISTS registry_source TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
