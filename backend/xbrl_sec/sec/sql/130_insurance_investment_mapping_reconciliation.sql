-- Reconcile insurance investment-portfolio facts away from corporate
-- cash-like short_term_investments.
--
-- For insurers, AFS / held-to-maturity / summary investment portfolio facts
-- are operating investment securities. Letting them inherit the broad
-- non_bank_financial short_term_investments mappings distorts derived net debt
-- because the corporate formula subtracts short-term investments as excess cash.

SET search_path TO sec, public;

INSERT INTO map_concept_to_taxonomy_versioned (
    concept_id,
    target_variable,
    tier,
    multiplier,
    reasoning,
    mapping_sector,
    jurisdiction,
    effective_from_year,
    effective_to_year,
    taxonomy_version,
    accounting_standard,
    review_status,
    mapping_source,
    confidence,
    gics_sector,
    gics_industry_group,
    gics_industry,
    gics_sub_industry,
    suggestion_id,
    source_method,
    source_confidence,
    approved_at,
    approved_by,
    aggregation_type,
    aggregation_priority,
    sign_policy,
    normal_balance,
    source_linkbase_evidence
)
SELECT
    m.concept_id,
    'investment_securities' AS target_variable,
    m.tier,
    m.multiplier,
    'Insurance sector override: investment-portfolio facts should reconcile to investment_securities, not corporate cash-like short_term_investments.' AS reasoning,
    'insurance' AS mapping_sector,
    m.jurisdiction,
    m.effective_from_year,
    m.effective_to_year,
    m.taxonomy_version,
    m.accounting_standard,
    COALESCE(m.review_status, 'approved') AS review_status,
    'sector_override' AS mapping_source,
    COALESCE(m.confidence, 0.82) AS confidence,
    m.gics_sector,
    '4030' AS gics_industry_group,
    m.gics_industry,
    m.gics_sub_industry,
    m.suggestion_id,
    COALESCE(m.source_method, 'insurance_mapping_reconciliation') AS source_method,
    COALESCE(m.source_confidence, m.confidence, 0.82) AS source_confidence,
    COALESCE(m.approved_at, now()) AS approved_at,
    COALESCE(m.approved_by, 'system') AS approved_by,
    m.aggregation_type,
    m.aggregation_priority,
    m.sign_policy,
    m.normal_balance,
    m.source_linkbase_evidence
FROM map_concept_to_taxonomy_versioned m
WHERE m.target_variable = 'short_term_investments'
  AND m.jurisdiction IN ('US', 'BOTH')
  AND COALESCE(m.mapping_sector, '') = 'non_bank_financial'
  AND (
       m.concept_id ILIKE '%AvailableForSale%'
    OR m.concept_id ILIKE '%DebtSecurities%'
    OR m.concept_id ILIKE '%HeldToMaturity%'
    OR m.concept_id ILIKE '%SummaryOfInvestments%'
    OR m.concept_id ILIKE '%RestrictedInvestments%'
    OR m.concept_id ILIKE '%SecuritiesHeldAsCollateral%'
    OR m.concept_id ILIKE '%FixedMaturity%'
  )
  AND NOT EXISTS (
      SELECT 1
      FROM map_concept_to_taxonomy_versioned existing
      WHERE existing.concept_id = m.concept_id
        AND existing.jurisdiction = m.jurisdiction
        AND COALESCE(existing.mapping_sector, '') = 'insurance'
        AND existing.target_variable = 'investment_securities'
  );

