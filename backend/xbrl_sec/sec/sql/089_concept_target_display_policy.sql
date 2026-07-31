-- Durable source-selection policy for mapped concepts.
--
-- Production mappings answer: raw concept -> standardized line item.
-- This table answers: can that raw concept be used as the main displayed
-- value for that standardized line item?

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS concept_target_display_policy (
    policy_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    normalized_concept_id TEXT NOT NULL,
    target_variable TEXT NOT NULL DEFAULT '',
    mapping_sector TEXT NOT NULL DEFAULT '',
    gics_sector TEXT,
    gics_industry_group TEXT,
    accounting_standard TEXT,
    taxonomy_version TEXT,
    fiscal_year_from SMALLINT,
    fiscal_year_to SMALLINT,
    fiscal_period TEXT,
    policy_action TEXT NOT NULL
        CHECK (policy_action IN (
            'allow_main',
            'prefer_main',
            'fallback_only',
            'component_only',
            'supplemental_only',
            'audit_only',
            'deny_main',
            'mapping_change_candidate',
            'needs_review'
        )),
    default_visibility TEXT NOT NULL DEFAULT 'default'
        CHECK (default_visibility IN ('default', 'supplemental', 'audit_only', 'hidden')),
    source_rank_penalty INTEGER NOT NULL DEFAULT 0,
    reason_code TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_queue_id BIGINT REFERENCES map_concept_to_taxonomy_review_queue(queue_id) ON DELETE SET NULL,
    mapping_source TEXT NOT NULL DEFAULT 'deterministic_concept_target_policy_v1',
    review_status TEXT NOT NULL DEFAULT 'generated'
        CHECK (review_status IN ('generated', 'reviewed', 'approved', 'rejected', 'expired')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_concept_target_display_policy_active
    ON concept_target_display_policy (
        jurisdiction,
        normalized_concept_id,
        COALESCE(target_variable, ''),
        COALESCE(mapping_sector, ''),
        COALESCE(gics_sector, ''),
        COALESCE(gics_industry_group, ''),
        COALESCE(accounting_standard, ''),
        COALESCE(taxonomy_version, ''),
        COALESCE(fiscal_year_from, -32768),
        COALESCE(fiscal_year_to, 32767),
        COALESCE(fiscal_period, ''),
        COALESCE(mapping_source, '')
    )
    WHERE active;

CREATE INDEX IF NOT EXISTS idx_concept_target_display_policy_lookup
    ON concept_target_display_policy (
        jurisdiction,
        normalized_concept_id,
        target_variable,
        mapping_sector,
        active,
        default_visibility,
        source_rank_penalty
    );

CREATE INDEX IF NOT EXISTS idx_concept_target_display_policy_action
    ON concept_target_display_policy (
        jurisdiction,
        policy_action,
        default_visibility,
        active
    );

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
       default_visibility,
       source_rank_penalty,
       reason_code,
       evidence,
       source_queue_id,
       mapping_source,
       review_status,
       CASE
           WHEN target_variable IS NOT NULL AND target_variable <> '' THEN 20 ELSE 0
       END
       + CASE WHEN mapping_sector IS NOT NULL AND mapping_sector <> '' THEN 10 ELSE 0 END
       + CASE WHEN gics_industry_group IS NOT NULL AND gics_industry_group <> '' THEN 4 ELSE 0 END
       + CASE WHEN gics_sector IS NOT NULL AND gics_sector <> '' THEN 2 ELSE 0 END
       + CASE WHEN accounting_standard IS NOT NULL AND accounting_standard <> '' THEN 2 ELSE 0 END
       + CASE WHEN taxonomy_version IS NOT NULL AND taxonomy_version <> '' THEN 1 ELSE 0 END
       + CASE WHEN fiscal_year_from IS NOT NULL OR fiscal_year_to IS NOT NULL THEN 1 ELSE 0 END
           AS specificity_rank
FROM concept_target_display_policy
WHERE active
  AND review_status IN ('generated', 'reviewed', 'approved');

COMMENT ON TABLE concept_target_display_policy IS
    'Durable concept-target source-selection policy. Does not replace production mappings; controls whether mapped source concepts may populate main display rows.';
