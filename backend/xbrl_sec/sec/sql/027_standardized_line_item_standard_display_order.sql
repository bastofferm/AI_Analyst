-- Per-accounting-standard display ordering for standardized line items.
--
-- Some canonical line items are placed differently under US GAAP and JP GAAP
-- model statements.  Keep the runtime source of truth in
-- ref_standardized_line_items while allowing dashboards to sort by the active
-- accounting standard.

SET search_path TO sec, public;

ALTER TABLE ref_standardized_line_items
    ADD COLUMN IF NOT EXISTS display_order_us_gaap INTEGER,
    ADD COLUMN IF NOT EXISTS display_order_jp_gaap INTEGER;

COMMENT ON COLUMN ref_standardized_line_items.display_order_us_gaap IS
    'US GAAP statement display order used by dashboards.';
COMMENT ON COLUMN ref_standardized_line_items.display_order_jp_gaap IS
    'JP GAAP statement display order used by dashboards.';

CREATE INDEX IF NOT EXISTS idx_rsli_statement_display_order_us
    ON ref_standardized_line_items (statement_type, sector_scope, display_order_us_gaap, line_item_id);

CREATE INDEX IF NOT EXISTS idx_rsli_statement_display_order_jp
    ON ref_standardized_line_items (statement_type, sector_scope, display_order_jp_gaap, line_item_id);
