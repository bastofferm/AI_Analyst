-- 133_noncurrent_asset_mapping_aliases.sql
--
-- Reconcile the stale tier-1 "noncurrent_assets" mapping target with the
-- canonical standardized aggregate id. "noncurrent_assets" is a balance-sheet
-- section label, not a registered line item in ref_standardized_line_items and
-- not a child in ref_std_item_edge. Facts mapped there become invisible to the
-- hierarchy closure pass.
--
-- Do not bulk-retarget tier-2 section/subtotal concepts to
-- other_noncurrent_assets here: many filer concepts named "OtherAssets..." are
-- presentation buckets that overlap with note-level children already mapped to
-- specific canonical leaves. The top-down residual closure pass is the safer
-- source for other_noncurrent_assets until M:1 subtotal semantics are explicit.

SET search_path TO sec, public;

UPDATE map_concept_to_taxonomy_versioned
SET target_variable = 'total_noncurrent_assets',
    updated_at = now()
WHERE jurisdiction IN ('US', 'BOTH')
  AND target_variable = 'noncurrent_assets'
  AND tier = 1;

UPDATE map_concept_to_taxonomy_exception
SET target_variable = 'total_noncurrent_assets',
    updated_at = now()
WHERE jurisdiction IN ('US', 'BOTH')
  AND target_variable = 'noncurrent_assets'
  AND tier = 1;

UPDATE map_concept_to_taxonomy
SET target_variable = 'total_noncurrent_assets'
WHERE target_variable = 'noncurrent_assets'
  AND tier = 1;
