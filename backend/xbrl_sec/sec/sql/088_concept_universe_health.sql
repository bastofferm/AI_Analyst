-- Whole concept-universe deterministic health review.
--
-- This extends review staging metadata only. It does not change raw facts,
-- standardized facts, production mappings, exceptions, or display profiles.

SET search_path TO sec, public;

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS map_concept_to_taxonomy_review_queue_review_class_check;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT map_concept_to_taxonomy_review_queue_review_class_check
    CHECK (review_class IN (
        'map_candidate',
        'special_case_review',
        'likely_exclude',
        'mapped_anomaly',
        'mapped_clean',
        'unmapped_candidate',
        'audit_only',
        'display_suppressed_candidate',
        'core_fundamental',
        'supplemental_numeric',
        'nonfundamental_disclosure',
        'rollforward_component',
        'rate_or_ratio',
        'table_member_noise',
        'unmappable_noise'
    ));

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS map_concept_to_taxonomy_review_queue_proposed_action_check;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT map_concept_to_taxonomy_review_queue_proposed_action_check
    CHECK (
        proposed_action IS NULL OR proposed_action IN (
            'keep',
            'global_mapping',
            'sector_scope',
            'year_scope',
            'sign_fix',
            'company_exception',
            'unmap',
            'supplemental_only',
            'alternate_total',
            'component_scope',
            'needs_review'
        )
    );

CREATE INDEX IF NOT EXISTS idx_mctrq_concept_health
    ON map_concept_to_taxonomy_review_queue (
        jurisdiction,
        review_class,
        triage_priority,
        reporter_count DESC,
        fact_count DESC
    )
    WHERE mapping_source LIKE 'concept_universe_health_v1:%';

DROP VIEW IF EXISTS vw_concept_universe_health_triage;

CREATE VIEW vw_concept_universe_health_triage AS
WITH ranked AS (
    SELECT q.queue_id,
           q.jurisdiction,
           q.review_class AS health_lane,
           q.normalized_concept_id,
           q.mapping_sector,
           q.gics_scope,
           q.gics_sector,
           q.gics_industry_group,
           q.current_mapping_id,
           q.proposed_action,
           q.review_action_type,
           q.triage_priority,
           q.review_batch AS coarse_review_batch,
           q.fact_count,
           q.filing_count,
           q.reporter_count,
           q.fiscal_year_min,
           q.fiscal_year_max,
           q.statement_types,
           q.taxonomies,
           q.accounting_standards,
           q.units,
           q.root_ids,
           q.parent_ids,
           q.concept_paths,
           q.sample_entities,
           q.sample_filings,
           q.context_role_distribution,
           q.evidence->'current_mapping'->>'target_variable' AS current_target_variable,
           NULLIF(q.evidence->'mapping_coverage'->>'coverage_ratio', '')::numeric AS mapping_coverage_ratio,
           q.evidence->>'health_reason' AS health_reason,
           q.evidence,
           ROW_NUMBER() OVER (
               PARTITION BY q.jurisdiction, q.review_class
               ORDER BY COALESCE(q.triage_priority, 99),
                        q.reporter_count DESC,
                        q.fact_count DESC,
                        q.queue_id
           ) AS lane_rank,
           ROW_NUMBER() OVER (
               PARTITION BY q.jurisdiction
               ORDER BY COALESCE(q.triage_priority, 99),
                        q.reporter_count DESC,
                        q.fact_count DESC,
                        q.queue_id
           ) AS jurisdiction_rank
    FROM map_concept_to_taxonomy_review_queue q
    WHERE q.mapping_source LIKE 'concept_universe_health_v1:%'
)
SELECT r.*,
       CASE
           WHEN r.health_lane = 'mapped_anomaly'
                AND (
                    r.review_action_type IN (
                        'sign_fix_candidate',
                        'alternate_total_fallback',
                        'sector_mapping_split'
                    )
                    OR COALESCE(r.triage_priority, 99) <= 5
                )
               THEN 'batch_1_mapped_anomaly_high_signal'
           WHEN r.health_lane = 'unmapped_candidate' AND r.lane_rank <= 100
               THEN 'batch_2_unmapped_candidate_top100'
           WHEN r.health_lane = 'display_suppressed_candidate' AND r.lane_rank <= 100
               THEN 'batch_3_display_suppressed_top100'
           WHEN r.health_lane = 'audit_only' AND r.lane_rank <= 50
               THEN 'batch_4_audit_only_spotcheck_top50'
           WHEN r.health_lane = 'mapped_clean' AND r.lane_rank <= 50
               THEN 'batch_5_mapped_clean_spotcheck_top50'
           ELSE 'backlog'
       END AS deterministic_review_batch
FROM ranked r;

COMMENT ON VIEW vw_concept_universe_health_triage IS
    'Review triage for concept_universe_health_v1 rows. This is deterministic staging evidence only.';
