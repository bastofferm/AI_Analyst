-- Context-aware mapped anomaly triage for deterministic review.

SET search_path TO sec, public;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD COLUMN IF NOT EXISTS context_role_distribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS review_action_type TEXT,
    ADD COLUMN IF NOT EXISTS triage_priority SMALLINT,
    ADD COLUMN IF NOT EXISTS review_batch TEXT;

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS map_concept_to_taxonomy_review_queue_review_action_type_check;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT map_concept_to_taxonomy_review_queue_review_action_type_check
    CHECK (
        review_action_type IS NULL OR review_action_type IN (
            'display_supplemental_only',
            'alternate_total_fallback',
            'component_only',
            'sector_mapping_split',
            'sign_fix_candidate',
            'keep',
            'needs_review'
        )
    );

CREATE INDEX IF NOT EXISTS idx_mctrq_review_action
    ON map_concept_to_taxonomy_review_queue (jurisdiction, review_class, review_action_type, triage_priority)
    WHERE review_class = 'mapped_anomaly';

COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.context_role_distribution IS
    'Counts of mapped fact contexts by role: primary_statement, note_disclosure, segment_or_schedule, cash_flow_addback, dimension_heavy, unknown.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.review_action_type IS
    'Review-oriented outcome type used by triage. It is advisory and never promotes a mapping by itself.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.triage_priority IS
    'Lower number means higher review priority in vw_mapping_anomaly_review_triage.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.review_batch IS
    'Coarse deterministic batch label for staged review.';

DROP VIEW IF EXISTS vw_mapping_anomaly_review_triage;

CREATE OR REPLACE VIEW vw_mapping_anomaly_review_triage AS
WITH ranked AS (
    SELECT q.queue_id,
           q.jurisdiction,
           q.normalized_concept_id,
           q.mapping_sector,
           q.current_mapping_id,
           q.concept_role,
           q.role_confidence,
           q.proposed_action,
           q.review_action_type,
           q.triage_priority,
           q.review_batch AS coarse_review_batch,
           q.fact_count,
           q.filing_count,
           q.reporter_count,
           q.fiscal_year_min,
           q.fiscal_year_max,
           q.failed_check_ids,
           q.identity_sides,
           q.residual_improvement_pct,
           q.counterfactual_best_action,
           q.context_role_distribution,
           q.sample_entities,
           q.sample_filings,
           q.evidence->>'anomaly_type' AS anomaly_type,
           q.evidence->'current_mapping'->>'target_variable' AS current_target_variable,
           q.evidence,
           ROW_NUMBER() OVER (
               PARTITION BY q.jurisdiction, q.concept_role, q.proposed_action
               ORDER BY q.reporter_count DESC, q.fact_count DESC, q.queue_id
           ) AS role_action_rank,
           ROW_NUMBER() OVER (
               PARTITION BY q.jurisdiction,
                            CASE
                                WHEN q.concept_role IN ('component', 'contra_component')
                                     AND q.review_action_type <> 'sign_fix_candidate'
                                    THEN 'component_or_contra_backlog'
                                ELSE 'other'
                            END
               ORDER BY q.reporter_count DESC, q.fact_count DESC, q.queue_id
           ) AS component_impact_rank,
           ROW_NUMBER() OVER (
               PARTITION BY q.jurisdiction
               ORDER BY COALESCE(q.triage_priority, 99), q.reporter_count DESC, q.fact_count DESC, q.queue_id
           ) AS jurisdiction_rank
    FROM map_concept_to_taxonomy_review_queue q
    WHERE q.review_class = 'mapped_anomaly'
      AND q.mapping_source LIKE 'mapped_anomaly_health_v3:%'
)
SELECT r.*,
       CASE
           WHEN r.review_action_type = 'sign_fix_candidate'
                OR r.review_action_type = 'alternate_total_fallback'
                OR (r.concept_role = 'disclosure_only' AND r.proposed_action = 'sector_scope')
               THEN 'A_high_signal'
           WHEN r.jurisdiction = 'US'
                AND r.concept_role = 'disclosure_only'
                AND r.proposed_action = 'supplemental_only'
                AND r.role_action_rank <= 50
               THEN 'B_us_disclosure_top50'
           WHEN r.concept_role IN ('component', 'contra_component')
                AND r.component_impact_rank <= 50
               THEN 'C_component_top50'
           WHEN r.concept_role = 'primary_total' AND r.proposed_action = 'keep'
               THEN 'D_primary_total_keep'
           ELSE 'backlog'
       END AS deterministic_review_batch
FROM ranked r;
