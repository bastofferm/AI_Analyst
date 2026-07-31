-- Fix the per-entity gap audit:
--   1. Add `is_filed = TRUE` to the unfilled-line-item lane of
--      v_entity_mapping_gap so items that are by-design computed downstream
--      (metrics layer: ROA/ROE/TBVPS/loss_ratio/combined_ratio/FFO/AFFO/...,
--      or assembly.py bridges: total_operating_expenses/total_D&A/etc.) stop
--      counting as mapping gaps.
--   2. Correct the is_filed flag on line items where real raw XBRL concepts
--      DO map to them and the flag was wrongly set to FALSE. These items
--      should appear in the gap audit as legitimate mapping targets.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- 1. Fix mislabeled line items (was is_filed=false; should be is_filed=true)
-- ---------------------------------------------------------------------------

UPDATE ref_standardized_line_items
SET is_filed = TRUE
WHERE line_item_id IN (
    -- Bank items that are filed by some entities
    'fee_income',
    'net_charge_offs',
    'amortization_of_core_deposit_intangibles',
    'fdic_insurance_expense',
    -- Insurance items filed by P&C / life insurers
    'gross_premiums_written',
    'ceded_premiums_written',
    'catastrophe_losses',
    'change_in_policy_benefit_reserves',
    'interest_credited_on_policyholder_account_balances',
    -- REIT items filed by REITs (recently authored mappings target these)
    'property_operating_expenses',
    'straight_line_rent_adjustment'
);

-- ---------------------------------------------------------------------------
-- 2. Update the view to add the is_filed filter on the unfilled lane.
--    All other CTE logic is unchanged from migration 100.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS v_entity_mapping_gap;

CREATE VIEW v_entity_mapping_gap AS
WITH
us_entities AS (
    SELECT
        d.cik AS entity_id,
        d.primary_ticker AS ticker,
        COALESCE(d.mapping_sector, 'corp') AS mapping_sector,
        d.gics_sector_code,
        d.gics_industry_group_code,
        CASE
            WHEN COALESCE(d.mapping_sector, 'corp') = 'bank_financial' THEN 'bank_financial'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' AND d.gics_industry_group_code = '4030' THEN 'insurance'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' AND d.gics_industry_group_code = '6010' THEN 'reit'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' THEN 'asset_manager_other_financial'
            ELSE 'corp'
        END AS sector_scope,
        'US_GAAP'::text AS accounting_standard,
        'US'::text AS jurisdiction
    FROM dim_company_us d
    WHERE COALESCE(d.include_in_pipeline, FALSE)
),
jp_entities AS (
    SELECT
        d.edinet_code AS entity_id,
        d.primary_ticker AS ticker,
        COALESCE(d.mapping_sector, 'corp') AS mapping_sector,
        d.gics_sector_code,
        d.gics_industry_group_code,
        CASE
            WHEN COALESCE(d.mapping_sector, 'corp') = 'bank_financial' THEN 'bank_financial'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' AND d.gics_industry_group_code = '4030' THEN 'insurance'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' AND d.gics_industry_group_code = '6010' THEN 'reit'
            WHEN COALESCE(d.mapping_sector, 'corp') = 'non_bank_financial' THEN 'asset_manager_other_financial'
            ELSE 'corp'
        END AS sector_scope,
        'JP_GAAP'::text AS accounting_standard,
        'JP'::text AS jurisdiction
    FROM dim_company_jp d
    WHERE COALESCE(d.include_in_pipeline, FALSE)
),
entity_periods_us AS (
    SELECT DISTINCT s.cik AS entity_id, s.fiscal_year, s.fiscal_period
    FROM fact_fundamentals_std_us s
    WHERE s.fiscal_year IS NOT NULL AND s.fiscal_period IS NOT NULL
),
entity_periods_jp AS (
    SELECT DISTINCT s.edinet_code AS entity_id, s.fiscal_year, s.fiscal_period
    FROM fact_fundamentals_std_jp s
    WHERE s.fiscal_year IS NOT NULL AND s.fiscal_period IS NOT NULL
),
us_unfilled AS (
    SELECT
        e.jurisdiction, e.entity_id, e.ticker, e.mapping_sector, e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        ep.fiscal_year, ep.fiscal_period,
        'unfilled_line_item'::text AS gap_kind,
        dp.line_item_id, dp.statement_type, dp.display_role, dp.display_policy,
        NULL::text AS concept_id, NULL::text AS normalized_concept_id,
        0::bigint AS fact_count,
        NULL::text AS evidence_calc_parent_concept_id,
        NULL::text AS evidence_calc_parent_target,
        NULL::numeric AS evidence_calc_parent_weight,
        ARRAY[]::text[] AS sample_filing_ids
    FROM us_entities e
    JOIN entity_periods_us ep ON ep.entity_id = e.entity_id
    JOIN ref_std_statement_display_profile dp
        ON dp.accounting_standard = e.accounting_standard
       AND dp.sector_scope = e.sector_scope
    JOIN ref_standardized_line_items r
        ON r.line_item_id = dp.line_item_id
    WHERE dp.display_role <> 'CALCULATED'
      AND dp.display_policy <> 'HIDE'
      AND COALESCE(r.is_filed, FALSE) = TRUE
      AND NOT EXISTS (
          SELECT 1 FROM fact_fundamentals_std_us s
          WHERE s.cik = e.entity_id
            AND s.fiscal_year = ep.fiscal_year
            AND s.fiscal_period = ep.fiscal_period
            AND s.line_item_id = dp.line_item_id
      )
),
jp_unfilled AS (
    SELECT
        e.jurisdiction, e.entity_id, e.ticker, e.mapping_sector, e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        ep.fiscal_year, ep.fiscal_period,
        'unfilled_line_item'::text AS gap_kind,
        dp.line_item_id, dp.statement_type, dp.display_role, dp.display_policy,
        NULL::text AS concept_id, NULL::text AS normalized_concept_id,
        0::bigint AS fact_count,
        NULL::text AS evidence_calc_parent_concept_id,
        NULL::text AS evidence_calc_parent_target,
        NULL::numeric AS evidence_calc_parent_weight,
        ARRAY[]::text[] AS sample_filing_ids
    FROM jp_entities e
    JOIN entity_periods_jp ep ON ep.entity_id = e.entity_id
    JOIN ref_std_statement_display_profile dp
        ON dp.accounting_standard = e.accounting_standard
       AND dp.sector_scope = e.sector_scope
    JOIN ref_standardized_line_items r
        ON r.line_item_id = dp.line_item_id
    WHERE dp.display_role <> 'CALCULATED'
      AND dp.display_policy <> 'HIDE'
      AND COALESCE(r.is_filed, FALSE) = TRUE
      AND NOT EXISTS (
          SELECT 1 FROM fact_fundamentals_std_jp s
          WHERE s.edinet_code = e.entity_id
            AND s.fiscal_year = ep.fiscal_year
            AND s.fiscal_period = ep.fiscal_period
            AND s.line_item_id = dp.line_item_id
      )
),
mapped_concepts AS (
    SELECT DISTINCT
        m.jurisdiction,
        COALESCE(m.mapping_sector, 'BOTH') AS scope,
        m.concept_id
    FROM map_concept_to_taxonomy_versioned m
),
us_unmapped AS (
    SELECT
        e.jurisdiction, e.entity_id, e.ticker, e.mapping_sector, e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        f.fiscal_year, f.fiscal_period,
        'unmapped_concept'::text AS gap_kind,
        NULL::text AS line_item_id, NULL::text AS statement_type,
        NULL::text AS display_role, NULL::text AS display_policy,
        f.concept_id, f.concept_id AS normalized_concept_id,
        COUNT(*) AS fact_count,
        NULL::text AS evidence_calc_parent_concept_id,
        NULL::text AS evidence_calc_parent_target,
        NULL::numeric AS evidence_calc_parent_weight,
        (ARRAY_AGG(DISTINCT f.filing_id))[1:5] AS sample_filing_ids
    FROM us_entities e
    JOIN fact_fundamentals_us f ON f.cik = e.entity_id
    WHERE f.fiscal_year IS NOT NULL
      AND f.fiscal_period IS NOT NULL
      AND f.value_type = 'ORIG'
      AND f.value IS NOT NULL
      AND f.concept_id NOT LIKE '%TextBlock'
      AND f.concept_id NOT LIKE '%Abstract'
      AND f.concept_id NOT LIKE '%Axis'
      AND f.concept_id NOT LIKE '%Domain'
      AND f.concept_id NOT LIKE '%Member'
      AND f.concept_id NOT LIKE '%Table'
      AND f.concept_id NOT LIKE '%LineItems'
      AND f.concept_id NOT LIKE '%RollForward'
      -- Tax reconciliation concepts are footnote effective-tax-rate components,
      -- not income-statement line items. They map to the metrics layer (effective
      -- tax rate disclosure) not to income_tax_provision.
      AND f.concept_id NOT LIKE '%IncomeTaxReconciliation%'
      AND NOT EXISTS (
          SELECT 1 FROM mapped_concepts mc
          WHERE mc.jurisdiction IN ('US', 'BOTH')
            AND mc.scope IN ('BOTH', e.mapping_sector)
            AND mc.concept_id = f.concept_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM map_concept_to_taxonomy_exception ex
          WHERE ex.jurisdiction = 'US'
            AND ex.entity_id = e.entity_id
            AND ex.concept_id = f.concept_id
            AND COALESCE(ex.review_status, 'approved') IN ('approved', 'queued')
            AND f.fiscal_year >= ex.fiscal_year_from
            AND (ex.fiscal_year_to IS NULL OR f.fiscal_year <= ex.fiscal_year_to)
      )
    GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
),
jp_unmapped AS (
    SELECT
        e.jurisdiction, e.entity_id, e.ticker, e.mapping_sector, e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        f.fiscal_year, f.fiscal_period,
        'unmapped_concept'::text AS gap_kind,
        NULL::text AS line_item_id, NULL::text AS statement_type,
        NULL::text AS display_role, NULL::text AS display_policy,
        f.concept_id, f.concept_id AS normalized_concept_id,
        COUNT(*) AS fact_count,
        NULL::text AS evidence_calc_parent_concept_id,
        NULL::text AS evidence_calc_parent_target,
        NULL::numeric AS evidence_calc_parent_weight,
        (ARRAY_AGG(DISTINCT f.filing_id))[1:5] AS sample_filing_ids
    FROM jp_entities e
    JOIN fact_fundamentals_jp f ON f.edinet_code = e.entity_id
    WHERE f.fiscal_year IS NOT NULL
      AND f.fiscal_period IS NOT NULL
      AND f.value_type = 'ORIG'
      AND f.value IS NOT NULL
      AND f.concept_id NOT LIKE '%TextBlock'
      AND f.concept_id NOT LIKE '%Abstract'
      AND f.concept_id NOT LIKE '%Axis'
      AND f.concept_id NOT LIKE '%Domain'
      AND f.concept_id NOT LIKE '%Member'
      AND f.concept_id NOT LIKE '%Table'
      AND f.concept_id NOT LIKE '%LineItems'
      AND f.concept_id NOT LIKE '%RollForward'
      AND f.concept_id NOT LIKE '%IncomeTaxReconciliation%'
      AND NOT EXISTS (
          SELECT 1 FROM mapped_concepts mc
          WHERE mc.jurisdiction IN ('JP', 'BOTH')
            AND mc.scope IN ('BOTH', e.mapping_sector)
            AND mc.concept_id = f.concept_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM map_concept_to_taxonomy_exception ex
          WHERE ex.jurisdiction = 'JP'
            AND ex.entity_id = e.entity_id
            AND ex.concept_id = f.concept_id
            AND COALESCE(ex.review_status, 'approved') IN ('approved', 'queued')
            AND f.fiscal_year >= ex.fiscal_year_from
            AND (ex.fiscal_year_to IS NULL OR f.fiscal_year <= ex.fiscal_year_to)
      )
    GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
)
SELECT * FROM us_unfilled
UNION ALL SELECT * FROM jp_unfilled
UNION ALL SELECT * FROM us_unmapped
UNION ALL SELECT * FROM jp_unmapped;

COMMENT ON VIEW v_entity_mapping_gap IS
    'Per-entity mapping gap backlog. Unfilled lane is filtered to is_filed=TRUE items only (computed metrics and bridge-derived items are excluded by design). Tax-reconciliation concepts are excluded from the unmapped lane.';
