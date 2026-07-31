-- Role-aware mapped anomaly evidence for concept mapping review.

SET search_path TO sec, public;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD COLUMN IF NOT EXISTS concept_role TEXT,
    ADD COLUMN IF NOT EXISTS role_confidence NUMERIC CHECK (role_confidence IS NULL OR (role_confidence >= 0 AND role_confidence <= 1)),
    ADD COLUMN IF NOT EXISTS failed_check_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS identity_sides TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS residual_improvement_pct NUMERIC,
    ADD COLUMN IF NOT EXISTS counterfactual_best_action TEXT;

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

CREATE INDEX IF NOT EXISTS idx_mctrq_role_review
    ON map_concept_to_taxonomy_review_queue (jurisdiction, review_class, concept_role, proposed_action)
    WHERE review_class = 'mapped_anomaly';

COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.concept_role IS
    'Deterministic role classification for mapped anomalies: primary_total, primary_line_item, alternate_total, component, contra_component, disclosure_only, table_member_noise, audit_only.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.residual_improvement_pct IS
    'Estimated median residual improvement from the best tested counterfactual, usually sign flip or exclusion.';
