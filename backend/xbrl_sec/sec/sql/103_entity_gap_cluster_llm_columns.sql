-- Add LLM scoring columns to map_entity_gap_cluster so Step 3 DeepSeek
-- proposals land in the same row as the cluster's evidence.

SET search_path TO sec, public;

ALTER TABLE map_entity_gap_cluster
    ADD COLUMN IF NOT EXISTS llm_suggested_target_variable TEXT,
    ADD COLUMN IF NOT EXISTS llm_suggested_aggregation_type TEXT,
    ADD COLUMN IF NOT EXISTS llm_suggested_sign_policy TEXT,
    ADD COLUMN IF NOT EXISTS llm_confidence NUMERIC,
    ADD COLUMN IF NOT EXISTS llm_reasoning TEXT,
    ADD COLUMN IF NOT EXISTS llm_decision TEXT,
    ADD COLUMN IF NOT EXISTS llm_model_name TEXT,
    ADD COLUMN IF NOT EXISTS llm_scored_at TIMESTAMPTZ;

ALTER TABLE map_entity_gap_cluster
    DROP CONSTRAINT IF EXISTS chk_megc_llm_agg_type;
ALTER TABLE map_entity_gap_cluster
    ADD CONSTRAINT chk_megc_llm_agg_type CHECK (
        llm_suggested_aggregation_type IS NULL OR llm_suggested_aggregation_type IN (
            'ROOT','CHILD_SUM','DIRECT','FALLBACK_TOTAL','EXCLUDE'
        )
    );

ALTER TABLE map_entity_gap_cluster
    DROP CONSTRAINT IF EXISTS chk_megc_llm_sign_policy;
ALTER TABLE map_entity_gap_cluster
    ADD CONSTRAINT chk_megc_llm_sign_policy CHECK (
        llm_suggested_sign_policy IS NULL OR llm_suggested_sign_policy IN (
            'as_reported','flip','force_negative','force_positive'
        )
    );

ALTER TABLE map_entity_gap_cluster
    DROP CONSTRAINT IF EXISTS chk_megc_llm_decision;
ALTER TABLE map_entity_gap_cluster
    ADD CONSTRAINT chk_megc_llm_decision CHECK (
        llm_decision IS NULL OR llm_decision IN (
            'PROPOSE','UNMAPPED','NEEDS_REVIEW','SKIP_NOISE'
        )
    );

CREATE INDEX IF NOT EXISTS idx_megc_llm_scored
    ON map_entity_gap_cluster (cluster_batch, llm_decision, llm_confidence DESC NULLS LAST)
    WHERE llm_decision IS NOT NULL;
