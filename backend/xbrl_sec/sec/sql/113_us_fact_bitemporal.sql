-- Make fact_fundamentals_us bitemporal: retain every FILING's view of every
-- period instead of collapsing to one row per period.
--
-- WHY
-- -----------------------------------------------------------------------------
-- The old US primary key
--   (cik, concept_id, period_end, fiscal_period, context_tier, value_type)
-- has no filing identity. When a later 10-K re-reports a prior year as a
-- comparative, that fact is form 10-K -> value_type='ORIG' and collides with the
-- originally-filed row on the PK; the upsert overwrites value, destroying the
-- first-reported number (only restatement_counter is bumped). That is look-ahead
-- bias: an "as-of FY2022" read could return a value only knowable in 2024.
--
-- Adding filing_id to the key keeps each filing's vintage as its own row. The
-- source SEC companyfacts JSON already carries every filing's view (each fact has
-- its own accn/filed), so a full US re-parse repopulates the lost history.
--
-- This mirrors migration 003 (which did the same for fact_fundamentals_jp).
--
-- IDEMPOTENCY: scripts/apply_schema.py runs every sql/*.sql on each apply. The
-- DROP CONSTRAINT IF EXISTS -> dedup -> ADD PRIMARY KEY sequence (as in 003) is
-- safe to run repeatedly.

SET search_path TO sec, public;

-- filing_id becomes part of the key, so it must be non-null.
UPDATE fact_fundamentals_us
SET filing_id = ''
WHERE filing_id IS NULL;

ALTER TABLE fact_fundamentals_us ALTER COLUMN filing_id SET NOT NULL;

ALTER TABLE fact_fundamentals_us DROP CONSTRAINT IF EXISTS fact_fundamentals_us_pkey;

-- Collapse any rows that are duplicates under the NEW key (keep most-recently
-- updated). On a fresh bitemporal table this removes nothing; on the legacy
-- table it is a no-op because the old key was already narrower.
DELETE FROM fact_fundamentals_us f
USING (
    SELECT ctid
    FROM (
        SELECT ctid,
               row_number() OVER (
                   PARTITION BY cik, filing_id, concept_id, period_end,
                                fiscal_period, context_tier, value_type
                   ORDER BY updated_at DESC, ctid DESC
               ) AS rn
        FROM fact_fundamentals_us
    ) ranked
    WHERE rn > 1
) dup
WHERE f.ctid = dup.ctid;

ALTER TABLE fact_fundamentals_us
    ADD PRIMARY KEY
        (cik, filing_id, concept_id, period_end, fiscal_period, context_tier, value_type);

COMMENT ON COLUMN fact_fundamentals_us.filing_id IS
    'SEC accession (accn) of the filing that reported this fact - the vintage '
    'axis. Part of the primary key: every filing''s view of a period is its own '
    'row, so original and later-restated comparatives coexist. Pick a vintage by '
    'filed_date (latest-known, or <= an as-of date). See v_fact_fundamentals_us_latest.';

-- Index supporting latest-vintage / as-of selection (newest filing per period).
CREATE INDEX IF NOT EXISTS idx_ff_us_period_vintage
    ON fact_fundamentals_us (cik, concept_id, period_end, fiscal_period, filed_date DESC);

-- -----------------------------------------------------------------------------
-- Latest-vintage surface: one row per (entity, concept, period, fp, value_type)
-- choosing the most-recently-filed view. This is the "current best knowledge"
-- 1:1 replacement for the old one-row-per-period table, plus the period-aligned
-- period_fiscal_year (see migration 112). Consumers that aggregate or join raw
-- should read this view to avoid multiplying rows across filings.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_fact_fundamentals_us_latest AS
SELECT DISTINCT ON (f.cik, f.concept_id, f.period_end, f.fiscal_period, f.value_type)
       f.*,
       CASE
           WHEN f.fiscal_period IN ('FY', 'Annual') AND f.period_end IS NOT NULL
               THEN EXTRACT(YEAR FROM f.period_end)::int
           ELSE f.fiscal_year
       END AS period_fiscal_year
FROM fact_fundamentals_us f
ORDER BY f.cik, f.concept_id, f.period_end, f.fiscal_period, f.value_type,
         f.filed_date DESC NULLS LAST, f.filing_id DESC;

COMMENT ON VIEW v_fact_fundamentals_us_latest IS
    'fact_fundamentals_us reduced to the latest-filed vintage per (cik, '
    'concept_id, period_end, fiscal_period, value_type), with period_fiscal_year. '
    'Use this for one-row-per-period "current best knowledge" reads; query the '
    'base table directly (filtering filed_date) for point-in-time / as-of vintages.';

-- -----------------------------------------------------------------------------
-- Recreate v_entity_mapping_gap (supersedes migration 102 and the 112 block).
-- The us_unmapped lane now reads v_fact_fundamentals_us_latest so a concept is
-- not counted once per filing vintage, and bins / matches exception windows by
-- the period-aligned fiscal year. JP lanes are verbatim from 102.
-- -----------------------------------------------------------------------------
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
        -- period-aligned fiscal year (filing-year column is unsafe for comparatives)
        f.period_fiscal_year AS fiscal_year,
        f.fiscal_period,
        'unmapped_concept'::text AS gap_kind,
        NULL::text AS line_item_id, NULL::text AS statement_type,
        NULL::text AS display_role, NULL::text AS display_policy,
        f.concept_id, f.concept_id AS normalized_concept_id,
        COUNT(*) AS fact_count,
        NULL::text AS evidence_calc_parent_concept_id,
        NULL::text AS evidence_calc_parent_target,
        NULL::numeric AS evidence_calc_parent_weight,
        (ARRAY_AGG(DISTINCT f.filing_id))[1:5] AS sample_filing_ids
    -- latest-vintage view: one row per period, not one per filing
    FROM us_entities e
    JOIN v_fact_fundamentals_us_latest f ON f.cik = e.entity_id
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
            AND f.period_fiscal_year >= ex.fiscal_year_from
            AND (ex.fiscal_year_to IS NULL OR f.period_fiscal_year <= ex.fiscal_year_to)
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
    'Per-entity mapping gap backlog. Unfilled lane is filtered to is_filed=TRUE items only (computed metrics and bridge-derived items are excluded by design). Tax-reconciliation concepts are excluded from the unmapped lane. US unmapped lane reads the latest-vintage view (one row per period, not per filing) and bins / matches exception windows by period-aligned fiscal year.';
