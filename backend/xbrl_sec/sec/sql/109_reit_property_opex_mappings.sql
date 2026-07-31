-- REIT content pass: two reviewed mappings that unlock the REIT bridges.
--
-- US GAAP has no standard NOI tag (NOI is non-GAAP), so net_operating_income
-- for US REITs comes from the display bridge: rental_revenue minus
-- property_operating_expenses. FFO/AFFO likewise need a filed real-estate
-- D&A figure. Evidence: SPG, O and AMT all file both concepts below
-- (checked against fact_fundamentals_us FY>=2023).
--
--   DirectCostsOfLeasedAndRentedPropertyOrEquipment  -- property opex
--   SECScheduleIIIRealEstateAccumulatedDepreciationDepreciationExpense
--                                                    -- real-estate D&A
--
-- FALLBACK_TOTAL for D&A so any better-sourced D&A wins when present.

SET search_path TO sec, public;

INSERT INTO map_concept_to_taxonomy_versioned
    (concept_id, target_variable, jurisdiction, mapping_sector,
     tier, multiplier, aggregation_type, sign_policy,
     aggregation_priority, effective_from_year, review_status, mapping_source)
SELECT v.*
FROM (VALUES
    ('us-gaap/DirectCostsOfLeasedAndRentedPropertyOrEquipment',
     'property_operating_expenses', 'US', 'non_bank_financial',
     1, 1::numeric, 'ROOT', 'as_reported', 10, 1900, 'promoted', 'reit_content_pass_202606'),
    ('us-gaap/SECScheduleIIIRealEstateAccumulatedDepreciationDepreciationExpense',
     'total_depreciation_and_amortization', 'US', 'non_bank_financial',
     1, 1::numeric, 'FALLBACK_TOTAL', 'as_reported', 100, 1900, 'promoted', 'reit_content_pass_202606')
) AS v(concept_id, target_variable, jurisdiction, mapping_sector,
       tier, multiplier, aggregation_type, sign_policy,
       aggregation_priority, effective_from_year, review_status, mapping_source)
WHERE NOT EXISTS (
    SELECT 1 FROM map_concept_to_taxonomy_versioned m
    WHERE m.concept_id = v.concept_id
      AND m.jurisdiction = v.jurisdiction
      AND COALESCE(m.mapping_sector,'') = v.mapping_sector
);
