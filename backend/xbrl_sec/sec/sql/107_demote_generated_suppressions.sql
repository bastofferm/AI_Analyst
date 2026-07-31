-- Demote machine-generated visibility suppressions to soft rank penalties.
--
-- Root cause of missing core statement lines (AAPL rendering with no
-- revenue, no gross profit, no income tax row): a bulk auto-generated
-- review pass wrote ~15.8K concept_target_display_policy rows with
-- policy_action='supplemental_only' / default_visibility='supplemental'
-- and review_status='generated' (never human-reviewed). The active view
-- treats 'generated' as live, and data.py's _fetch_std_rows applies
-- default_visibility as a HARD filter -- silently blanking the canonical
-- source concepts for nearly every core line:
--   NetCashProvidedByUsedInOperatingActivities (100% of CFO sources),
--   IncomeTaxExpenseBenefit (98%), CashAndCashEquivalentsAtCarryingValue
--   (92%), AccountsPayableCurrent (95%), LongTermDebt (92%),
--   RevenueFromContractWithCustomerExcludingAssessedTax, GrossProfit, ...
--
-- The _prefer_us_cost_of_revenue hack in statements/data.py is a symptom
-- patch for this same problem.
--
-- Fix: in the active view, generated (unreviewed) 'supplemental' visibility
-- is demoted to 'default'. The rows keep their source_rank_penalty, so they
-- still do their legitimate job -- pushing suspect sources (fair-value
-- disclosures, flow-vs-stock mismatches) below better sources in the
-- ROW_NUMBER ranking -- without ever blanking a line when they are the only
-- source. Human-reviewed/approved suppressions and audit_only rows keep
-- their hard-filter teeth.

SET search_path TO sec, public;

CREATE OR REPLACE VIEW vw_concept_target_display_policy_active AS
SELECT policy_id,
    jurisdiction,
    normalized_concept_id,
    target_variable,
    mapping_sector,
    gics_sector,
    gics_industry_group,
    accounting_standard,
    taxonomy_version,
    fiscal_year_from,
    fiscal_year_to,
    fiscal_period,
    policy_action,
    CASE
        WHEN review_status = 'generated' AND default_visibility = 'supplemental'
            THEN 'default'
        ELSE default_visibility
    END AS default_visibility,
    source_rank_penalty,
    reason_code,
    evidence,
    source_queue_id,
    mapping_source,
    review_status,
        CASE
            WHEN target_variable IS NOT NULL AND target_variable <> ''::text THEN 20
            ELSE 0
        END +
        CASE
            WHEN mapping_sector IS NOT NULL AND mapping_sector <> ''::text THEN 10
            ELSE 0
        END +
        CASE
            WHEN gics_industry_group IS NOT NULL AND gics_industry_group <> ''::text THEN 4
            ELSE 0
        END +
        CASE
            WHEN gics_sector IS NOT NULL AND gics_sector <> ''::text THEN 2
            ELSE 0
        END +
        CASE
            WHEN accounting_standard IS NOT NULL AND accounting_standard <> ''::text THEN 2
            ELSE 0
        END +
        CASE
            WHEN taxonomy_version IS NOT NULL AND taxonomy_version <> ''::text THEN 1
            ELSE 0
        END +
        CASE
            WHEN fiscal_year_from IS NOT NULL OR fiscal_year_to IS NOT NULL THEN 1
            ELSE 0
        END AS specificity_rank
   FROM concept_target_display_policy
  WHERE active AND (review_status = ANY (ARRAY['generated'::text, 'reviewed'::text, 'approved'::text]));

COMMENT ON VIEW vw_concept_target_display_policy_active IS
    'Active display policies. Generated (unreviewed) supplemental suppressions are demoted to default visibility with their rank penalty intact: they influence source ordering but can no longer blank a line. Reviewed/approved suppressions and audit_only rows remain hard filters.';
