-- v_entity_mapping_gap: per-entity backlog of mapping gaps.
--
-- Two row types per (jurisdiction, entity_id, fiscal_year, fiscal_period):
--   * unfilled_line_item  -- a display-profile slot expected for the entity's
--                            sector_scope but no standardized fact exists.
--   * unmapped_concept    -- a raw XBRL concept filed by the entity that did
--                            not produce a standardized row and is not covered
--                            by any sector-compatible mapping or active
--                            exception.
--
-- Linkbase calc-parent evidence (the parent concept this child rolls up to
-- in the filing's calculation linkbase) is attached for unmapped_concept
-- rows via ref_xbrl_relationship_edge so Step 2 clustering can split into
-- Lane A (linkbase-only auto-fillable) and Lane B (needs LLM scoring).

SET search_path TO sec, public;

DROP VIEW IF EXISTS v_entity_mapping_gap;

CREATE VIEW v_entity_mapping_gap AS
WITH
-- ---------------------------------------------------------------------------
-- Entity sector resolution (mirrors data.py:_display_sector)
-- ---------------------------------------------------------------------------
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
all_entities AS (
    SELECT * FROM us_entities
    UNION ALL
    SELECT * FROM jp_entities
),
-- ---------------------------------------------------------------------------
-- Entity-year-period universe: every (entity, fy, fp) that already produced
-- at least one standardized row. We only emit gaps for periods we know we
-- have data for; otherwise an entity that hasn't filed yet would look
-- maximally broken.
-- ---------------------------------------------------------------------------
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
-- ---------------------------------------------------------------------------
-- Unfilled line_items (gap_kind = 'unfilled_line_item')
-- ---------------------------------------------------------------------------
us_unfilled AS (
    SELECT
        e.jurisdiction,
        e.entity_id,
        e.ticker,
        e.mapping_sector,
        e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        ep.fiscal_year,
        ep.fiscal_period,
        'unfilled_line_item'::text AS gap_kind,
        dp.line_item_id,
        dp.statement_type,
        dp.display_role,
        dp.display_policy,
        NULL::text AS concept_id,
        NULL::text AS normalized_concept_id,
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
    WHERE dp.display_role <> 'CALCULATED'
      AND dp.display_policy <> 'HIDE'
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
        e.jurisdiction,
        e.entity_id,
        e.ticker,
        e.mapping_sector,
        e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        ep.fiscal_year,
        ep.fiscal_period,
        'unfilled_line_item'::text AS gap_kind,
        dp.line_item_id,
        dp.statement_type,
        dp.display_role,
        dp.display_policy,
        NULL::text AS concept_id,
        NULL::text AS normalized_concept_id,
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
    WHERE dp.display_role <> 'CALCULATED'
      AND dp.display_policy <> 'HIDE'
      AND NOT EXISTS (
          SELECT 1 FROM fact_fundamentals_std_jp s
          WHERE s.edinet_code = e.entity_id
            AND s.fiscal_year = ep.fiscal_year
            AND s.fiscal_period = ep.fiscal_period
            AND s.line_item_id = dp.line_item_id
      )
),
-- ---------------------------------------------------------------------------
-- Sector-compatible concept set per (jurisdiction, mapping_sector). A concept
-- counts as "covered for this sector" if at least one mapping row matches the
-- sector hierarchy (BOTH or sector-specific).
-- ---------------------------------------------------------------------------
mapped_concepts AS (
    SELECT DISTINCT
        m.jurisdiction,
        COALESCE(m.mapping_sector, 'BOTH') AS scope,
        m.concept_id
    FROM map_concept_to_taxonomy_versioned m
),
-- ---------------------------------------------------------------------------
-- Unmapped concepts per entity-year (gap_kind = 'unmapped_concept').
-- Skips noise-y suffixes at the XBRL level: TextBlock, Abstract, Axis, Domain,
-- Member, Table, LineItems, RollForward. Labels-based noise (mapping_suggestions
-- patterns) is applied later in the Python script.
-- ---------------------------------------------------------------------------
us_unmapped AS (
    SELECT
        e.jurisdiction,
        e.entity_id,
        e.ticker,
        e.mapping_sector,
        e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        f.fiscal_year,
        f.fiscal_period,
        'unmapped_concept'::text AS gap_kind,
        NULL::text AS line_item_id,
        NULL::text AS statement_type,
        NULL::text AS display_role,
        NULL::text AS display_policy,
        f.concept_id,
        f.concept_id AS normalized_concept_id,
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
        e.jurisdiction,
        e.entity_id,
        e.ticker,
        e.mapping_sector,
        e.sector_scope,
        e.gics_industry_group_code AS gics_industry_group,
        f.fiscal_year,
        f.fiscal_period,
        'unmapped_concept'::text AS gap_kind,
        NULL::text AS line_item_id,
        NULL::text AS statement_type,
        NULL::text AS display_role,
        NULL::text AS display_policy,
        f.concept_id,
        f.concept_id AS normalized_concept_id,
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
    'Per-entity backlog of mapping gaps. Two row types: unfilled_line_item (profile slot empty) and unmapped_concept (raw concept filed but not mapped). Step 1 of the long-tail mapping fix plan. Linkbase calc-parent evidence is attached by the Python audit script via a second pass against ref_xbrl_relationship_edge.';
