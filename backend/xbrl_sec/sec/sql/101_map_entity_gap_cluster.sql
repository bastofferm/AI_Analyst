-- map_entity_gap_cluster: deduplicated long-tail mapping gap backlog.
--
-- Each row is a cluster of v_entity_mapping_gap rows that share the same
-- (jurisdiction, mapping_sector, normalized_concept_id, inferred_target_line_item)
-- and therefore would be fixed by the same global mapping rule. Step 2 of
-- the long-tail mapping fix plan splits the backlog into:
--   * Lane A (linkbase_only_eligible = TRUE): calc-parent evidence is strong
--     enough to auto-fill without LLM scoring.
--   * Lane B (linkbase_only_eligible = FALSE): needs Step 3 DeepSeek scoring.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS map_entity_gap_cluster (
    cluster_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    mapping_sector TEXT NOT NULL DEFAULT 'corp',
    normalized_concept_id TEXT,              -- NULL for unfilled-only clusters
    inferred_target_line_item TEXT,          -- NULL when no calc-parent inference
    proposed_aggregation_type TEXT
        CHECK (proposed_aggregation_type IS NULL OR proposed_aggregation_type IN (
            'ROOT', 'CHILD_SUM', 'DIRECT', 'FALLBACK_TOTAL', 'EXCLUDE'
        )),
    proposed_sign_policy TEXT
        CHECK (proposed_sign_policy IS NULL OR proposed_sign_policy IN (
            'as_reported', 'flip', 'force_negative', 'force_positive'
        )),
    entity_count INT NOT NULL DEFAULT 0,
    total_fact_count BIGINT NOT NULL DEFAULT 0,
    sample_entity_ids TEXT[] NOT NULL DEFAULT '{}',
    sample_tickers TEXT[] NOT NULL DEFAULT '{}',
    calc_parent_concept_id TEXT,             -- most common calc parent across cluster
    calc_parent_support_pct NUMERIC,         -- entity_count_with_calc_parent / entity_count
    linkbase_only_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    entity_specificity TEXT NOT NULL DEFAULT 'narrow'
        CHECK (entity_specificity IN ('shared', 'narrow')),
    gap_kind TEXT NOT NULL DEFAULT 'unmapped_concept'
        CHECK (gap_kind IN ('unmapped_concept', 'unfilled_line_item', 'mixed')),
    cluster_batch TEXT,                      -- e.g. 'entity_gap_202606'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_megc_lane
    ON map_entity_gap_cluster (jurisdiction, linkbase_only_eligible, entity_count DESC);
CREATE INDEX IF NOT EXISTS idx_megc_concept
    ON map_entity_gap_cluster (jurisdiction, normalized_concept_id);
CREATE INDEX IF NOT EXISTS idx_megc_target
    ON map_entity_gap_cluster (jurisdiction, inferred_target_line_item);
CREATE UNIQUE INDEX IF NOT EXISTS uq_megc_natural_key
    ON map_entity_gap_cluster (
        jurisdiction,
        mapping_sector,
        COALESCE(normalized_concept_id, ''),
        COALESCE(inferred_target_line_item, ''),
        COALESCE(cluster_batch, '')
    );

COMMENT ON TABLE map_entity_gap_cluster IS
    'Deduplicated long-tail mapping gap backlog. Step 2 of the long-tail mapping fix plan. Lane A (linkbase_only_eligible=TRUE) is auto-fillable without LLM; Lane B is the input to Step 3 DeepSeek scoring.';
COMMENT ON COLUMN map_entity_gap_cluster.calc_parent_support_pct IS
    'Fraction of entities in the cluster where the calculation linkbase consistently lists the same parent concept. >= 0.80 + entity_count >= 10 = Lane A eligible.';
COMMENT ON COLUMN map_entity_gap_cluster.entity_specificity IS
    'shared = >= 5 entities (good for sector mapping); narrow = 1-4 entities (good for exception table).';
