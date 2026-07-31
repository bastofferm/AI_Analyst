-- Reconcile sector_scope naming in ref_standardized_line_items so that
-- GICS-coded buckets used by the line-item dictionary align with the
-- business-style names returned by data.py _display_sector. Without this,
-- bank/insurance/REIT/asset-manager items are unreachable through the
-- rendering path because the heuristic fallback in _fetch_profile_rows
-- filters by sector_scope IN ('universal', %s) where %s is the business
-- name, never the GICS code.

SET search_path TO sec, public;

-- Preserve the original GICS code for provenance so we can reconstruct
-- the GICS-based grouping if we ever want to (e.g. for cross-checks).
ALTER TABLE ref_standardized_line_items
    ADD COLUMN IF NOT EXISTS gics_sector_origin TEXT;

UPDATE ref_standardized_line_items
SET gics_sector_origin = sector_scope
WHERE sector_scope LIKE 'gics_%'
  AND gics_sector_origin IS NULL;

-- The actual rename. Only the four buckets that map cleanly to the
-- display sector_scopes are renamed. Others (gics_10, gics_20, gics_25,
-- gics_45, gics_50, gics_55) are KPI-only buckets the display layer
-- never asks for, so we leave them alone.
UPDATE ref_standardized_line_items
SET sector_scope = CASE sector_scope
    WHEN 'gics_40_banks'              THEN 'bank_financial'
    WHEN 'gics_40_insurance'          THEN 'insurance'
    WHEN 'gics_40_financial_services' THEN 'asset_manager_other_financial'
    WHEN 'gics_60'                    THEN 'reit'
    ELSE sector_scope
END
WHERE sector_scope IN (
    'gics_40_banks',
    'gics_40_insurance',
    'gics_40_financial_services',
    'gics_60'
);

COMMENT ON COLUMN ref_standardized_line_items.gics_sector_origin IS
    'Original GICS-coded sector_scope before reconciliation to business-style names (gics_40_banks->bank_financial etc.). NULL if the row was never GICS-coded.';
