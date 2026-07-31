-- REIT-specific sector overrides (second attempt; migration 109's inserts
-- were blocked by its NOT EXISTS guard because the concepts already carry
-- corp-style mappings: DirectCostsOfLeasedAndRentedPropertyOrEquipment ->
-- cost_of_goods_sold and SECScheduleIII...DepreciationExpense ->
-- real_estate_depreciation, both at mapping_sector='non_bank_financial').
--
-- _sector_order('reit') = ['reit', 'non_bank_financial', ''] resolves
-- mapping_sector='reit' rows FIRST for entities whose hierarchy sector is
-- reit (non_bank_financial + GICS 60). So these rows win for REITs only;
-- all other non-bank financials keep the existing treatment.
--
-- Unlocks the REIT display bridges: net_operating_income = rental_revenue -
-- property_operating_expenses; FFO = net_income + D&A; AFFO = FFO - capex.

SET search_path TO sec, public;

INSERT INTO map_concept_to_taxonomy_versioned
    (concept_id, target_variable, jurisdiction, mapping_sector,
     tier, multiplier, aggregation_type, sign_policy,
     aggregation_priority, effective_from_year, review_status, mapping_source)
SELECT v.*
FROM (VALUES
    ('us-gaap/DirectCostsOfLeasedAndRentedPropertyOrEquipment',
     'property_operating_expenses', 'US', 'reit',
     1, 1::numeric, 'ROOT', 'as_reported', 10, 1900, 'promoted', 'reit_content_pass_202606'),
    ('us-gaap/SECScheduleIIIRealEstateAccumulatedDepreciationDepreciationExpense',
     'total_depreciation_and_amortization', 'US', 'reit',
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
