-- Display ordering for standardized line items.
--
-- ref_standardized_line_items is the runtime source of truth for canonical
-- line-item metadata.  Hierarchy specs may seed this value, but dashboard
-- display order reads it from the DB.

SET search_path TO sec, public;

ALTER TABLE ref_standardized_line_items
    ADD COLUMN IF NOT EXISTS display_order INTEGER;

COMMENT ON COLUMN ref_standardized_line_items.display_order IS
    'Statement display order seeded from hierarchy specs and used by dashboards before importance/name fallbacks.';

CREATE INDEX IF NOT EXISTS idx_rsli_statement_display_order
    ON ref_standardized_line_items (statement_type, sector_scope, display_order, line_item_id);
