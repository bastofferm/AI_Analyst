-- Revert most of the entity_gap_llm_promotion_202606 batch.
--
-- Rationale (operator decision): extremely specialized concepts add little
-- information content and most belong in residual "other_*" lines derived
-- arithmetically (parent minus known siblings), not in first-class mappings.
-- On re-inspection most of the 34 promoted rows had unit (option counts vs
-- dollars), magnitude (exec comp vs total labor; notional in-force vs
-- premiums; authorization vs actual buybacks), or target (NII-after-provision
-- mapped to plain NII) problems.
--
-- Keep, with corrections:
--   1. InterestIncomeExpenseAfterProvisionForLoanLoss (bank) -- but fix the
--      target: the concept is NII *after provision*, so it must map to
--      net_interest_income_after_provision, not net_interest_income.
--   2. CostsIncurredOilAndGasPropertyAcquisitionExplorationAndDevelopment
--      Activities (corp) -- the standard E&P capex disclosure; downgraded to
--      FALLBACK_TOTAL so the normal capex concept wins when filed.
--   3. InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement
--      (non_bank) -- BDC total operating expense; downgraded to
--      FALLBACK_TOTAL for the same reason.
--
-- Everything else from the batch is deleted. The standardizer has not run
-- since promotion, so no standardized rows reference these mappings.

SET search_path TO sec, public;

-- 1. Fix the bank NII mapping target.
UPDATE map_concept_to_taxonomy_versioned
SET target_variable = 'net_interest_income_after_provision',
    updated_at = now()
WHERE mapping_source = 'entity_gap_llm_promotion_202606'
  AND concept_id = 'us-gaap/InterestIncomeExpenseAfterProvisionForLoanLoss'
  AND mapping_sector = 'bank_financial';

-- 2. Downgrade the two niche-but-sound keeps to FALLBACK_TOTAL.
UPDATE map_concept_to_taxonomy_versioned
SET aggregation_type = 'FALLBACK_TOTAL',
    aggregation_priority = 100,
    updated_at = now()
WHERE mapping_source = 'entity_gap_llm_promotion_202606'
  AND concept_id IN (
      'us-gaap/CostsIncurredOilAndGasPropertyAcquisitionExplorationAndDevelopmentActivities',
      'us-gaap/InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement'
  );

-- 3. Delete the other 31 promoted rows.
DELETE FROM map_concept_to_taxonomy_versioned
WHERE mapping_source = 'entity_gap_llm_promotion_202606'
  AND concept_id NOT IN (
      'us-gaap/InterestIncomeExpenseAfterProvisionForLoanLoss',
      'us-gaap/CostsIncurredOilAndGasPropertyAcquisitionExplorationAndDevelopmentActivities',
      'us-gaap/InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement'
  );

-- 4. Reset the reverted clusters so a re-run cannot silently re-promote them.
UPDATE map_entity_gap_cluster
SET llm_decision = 'NEEDS_REVIEW'
WHERE cluster_batch = 'entity_gap_202606_us_v2'
  AND llm_decision = 'PROMOTED'
  AND NOT (
      (normalized_concept_id = 'us-gaap/InterestIncomeExpenseAfterProvisionForLoanLoss' AND mapping_sector = 'bank_financial')
      OR normalized_concept_id IN (
          'us-gaap/CostsIncurredOilAndGasPropertyAcquisitionExplorationAndDevelopmentActivities',
          'us-gaap/InvestmentCompanyExpenseAfterReductionOfFeeWaiverAndReimbursement'
      )
  );
