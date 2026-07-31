-- Reconcile legacy mapping target names with the canonical profile names.
--
-- ~54 mapping rows point at legacy target names that exist nowhere else:
-- not in ref_standardized_line_items, not in any display profile, no edges,
-- no formulas. Their standardized output is therefore invisible. The data is
-- substantial: noi hides filed net operating income for 461 REITs;
-- premiums_earned hides the insurance top line for 111 insurers.
--
--   legacy target        -> canonical profile name              (US ents)
--   premiums_earned      -> net_premiums_earned                 111
--   claims_incurred      -> claims_and_losses_incurred          104
--   underwriting_income  -> underwriting_income_loss             16
--   underwriting_expense -> insurance_underwriting_expense       34
--   noi                  -> net_operating_income                461
--   management_fees      -> management_fee_revenue                1
--   noninterest_income   -> non_interest_income                 329
--
-- Stale std rows under the legacy names are cleaned up by the next scoped or
-- full re-standardize (entity-scoped runs DELETE before rewrite).

SET search_path TO sec, public;

UPDATE map_concept_to_taxonomy_versioned
SET target_variable = CASE target_variable
        WHEN 'premiums_earned'      THEN 'net_premiums_earned'
        WHEN 'claims_incurred'      THEN 'claims_and_losses_incurred'
        WHEN 'underwriting_income'  THEN 'underwriting_income_loss'
        WHEN 'underwriting_expense' THEN 'insurance_underwriting_expense'
        WHEN 'noi'                  THEN 'net_operating_income'
        WHEN 'management_fees'      THEN 'management_fee_revenue'
        WHEN 'noninterest_income'   THEN 'non_interest_income'
    END,
    updated_at = now()
WHERE target_variable IN (
    'premiums_earned', 'claims_incurred', 'underwriting_income',
    'underwriting_expense', 'noi', 'management_fees', 'noninterest_income'
);

-- Same for entity exceptions, if any reference the legacy names.
UPDATE map_concept_to_taxonomy_exception
SET target_variable = CASE target_variable
        WHEN 'premiums_earned'      THEN 'net_premiums_earned'
        WHEN 'claims_incurred'      THEN 'claims_and_losses_incurred'
        WHEN 'underwriting_income'  THEN 'underwriting_income_loss'
        WHEN 'underwriting_expense' THEN 'insurance_underwriting_expense'
        WHEN 'noi'                  THEN 'net_operating_income'
        WHEN 'management_fees'      THEN 'management_fee_revenue'
        WHEN 'noninterest_income'   THEN 'non_interest_income'
    END,
    updated_at = now()
WHERE target_variable IN (
    'premiums_earned', 'claims_incurred', 'underwriting_income',
    'underwriting_expense', 'noi', 'management_fees', 'noninterest_income'
);

-- Deduplicate: if retargeting created two rows for the same
-- (concept, jurisdiction, sector, target), keep the oldest.
DELETE FROM map_concept_to_taxonomy_versioned a
USING map_concept_to_taxonomy_versioned b
WHERE a.mapping_id > b.mapping_id
  AND a.concept_id = b.concept_id
  AND a.jurisdiction = b.jurisdiction
  AND COALESCE(a.mapping_sector,'') = COALESCE(b.mapping_sector,'')
  AND a.target_variable = b.target_variable;

-- net_operating_income now receives filed data from 461 REITs, but its
-- dictionary row says statement_type='derived' / is_filed=false, which
-- (a) excludes it from the income-statement display fetch and (b) hides it
-- from gap audits. Promote it to a filed income-statement item; the display
-- bridge still computes it for REITs that do not file it.
UPDATE ref_standardized_line_items
SET statement_type = 'income_statement',
    is_filed = TRUE
WHERE line_item_id = 'net_operating_income';

-- These two now receive filed data as well; flag them filed so audits and
-- profiles treat them as mappable rather than compute-only.
UPDATE ref_standardized_line_items
SET is_filed = TRUE
WHERE line_item_id IN ('net_premiums_earned', 'underwriting_income_loss');
