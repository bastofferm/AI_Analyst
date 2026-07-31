-- Phase 1: Explicit M:1 aggregation metadata on versioned concept mappings.
--
-- Adds self-documenting columns that replace the implicit tier/multiplier
-- semantics with reviewable aggregation intent. No behavioral changes:
-- standardizers still read tier and multiplier. These columns exist for
-- provenance, review tooling, and the future shared resolver.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- 1. Add aggregation metadata columns to production mapping table
-- ---------------------------------------------------------------------------

ALTER TABLE map_concept_to_taxonomy_versioned
    ADD COLUMN IF NOT EXISTS aggregation_type TEXT,
    ADD COLUMN IF NOT EXISTS aggregation_priority SMALLINT,
    ADD COLUMN IF NOT EXISTS sign_policy TEXT,
    ADD COLUMN IF NOT EXISTS normal_balance TEXT,
    ADD COLUMN IF NOT EXISTS source_linkbase_evidence JSONB;

-- Enum-like check constraints
ALTER TABLE map_concept_to_taxonomy_versioned
    DROP CONSTRAINT IF EXISTS chk_mctv_aggregation_type;
ALTER TABLE map_concept_to_taxonomy_versioned
    ADD CONSTRAINT chk_mctv_aggregation_type
    CHECK (aggregation_type IS NULL OR aggregation_type IN (
        'ROOT',
        'CHILD_SUM',
        'DIRECT',
        'FALLBACK_TOTAL',
        'EXCLUDE'
    ));

ALTER TABLE map_concept_to_taxonomy_versioned
    DROP CONSTRAINT IF EXISTS chk_mctv_sign_policy;
ALTER TABLE map_concept_to_taxonomy_versioned
    ADD CONSTRAINT chk_mctv_sign_policy
    CHECK (sign_policy IS NULL OR sign_policy IN (
        'as_reported',
        'flip',
        'force_negative',
        'force_positive'
    ));

ALTER TABLE map_concept_to_taxonomy_versioned
    DROP CONSTRAINT IF EXISTS chk_mctv_normal_balance;
ALTER TABLE map_concept_to_taxonomy_versioned
    ADD CONSTRAINT chk_mctv_normal_balance
    CHECK (normal_balance IS NULL OR normal_balance IN (
        'debit',
        'credit'
    ));

-- Column documentation
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.aggregation_type IS
    'Explicit aggregation role: ROOT (reported total, preferred), CHILD_SUM (component for summation), DIRECT (one-to-one, no component fallback), FALLBACK_TOTAL (lower-priority total), EXCLUDE (audit/display only).';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.aggregation_priority IS
    'Tie-breaker within same aggregation_type for a target. Lower value = higher priority. NULL treated as default priority.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.sign_policy IS
    'How to handle the sign: as_reported (use raw), flip (negate), force_negative (abs then negate), force_positive (abs). NULL defaults to as_reported.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.normal_balance IS
    'XBRL normal balance evidence: debit or credit. Informs sign interpretation. NULL means unknown or not applicable.';
COMMENT ON COLUMN map_concept_to_taxonomy_versioned.source_linkbase_evidence IS
    'JSON evidence from calculation/presentation/definition linkbases that informed this mapping row. NULL when no linkbase evidence was used.';

-- ---------------------------------------------------------------------------
-- 2. Backfill aggregation_type from existing tier and multiplier
-- ---------------------------------------------------------------------------

UPDATE map_concept_to_taxonomy_versioned
SET aggregation_type = CASE
        WHEN tier = 1 THEN 'ROOT'
        WHEN tier IS NOT NULL THEN 'CHILD_SUM'
        ELSE NULL
    END,
    sign_policy = CASE
        WHEN multiplier = -1 THEN 'flip'
        ELSE 'as_reported'
    END
WHERE aggregation_type IS NULL;

-- ---------------------------------------------------------------------------
-- 3. Add aggregation metadata columns to exception table
-- ---------------------------------------------------------------------------

ALTER TABLE map_concept_to_taxonomy_exception
    ADD COLUMN IF NOT EXISTS aggregation_type TEXT,
    ADD COLUMN IF NOT EXISTS sign_policy TEXT;

ALTER TABLE map_concept_to_taxonomy_exception
    DROP CONSTRAINT IF EXISTS chk_mcte_aggregation_type;
ALTER TABLE map_concept_to_taxonomy_exception
    ADD CONSTRAINT chk_mcte_aggregation_type
    CHECK (aggregation_type IS NULL OR aggregation_type IN (
        'ROOT',
        'CHILD_SUM',
        'DIRECT',
        'FALLBACK_TOTAL',
        'EXCLUDE'
    ));

ALTER TABLE map_concept_to_taxonomy_exception
    DROP CONSTRAINT IF EXISTS chk_mcte_sign_policy;
ALTER TABLE map_concept_to_taxonomy_exception
    ADD CONSTRAINT chk_mcte_sign_policy
    CHECK (sign_policy IS NULL OR sign_policy IN (
        'as_reported',
        'flip',
        'force_negative',
        'force_positive'
    ));

-- Backfill exceptions from their tier/multiplier
UPDATE map_concept_to_taxonomy_exception
SET aggregation_type = CASE
        WHEN tier = 1 THEN 'ROOT'
        ELSE 'CHILD_SUM'
    END,
    sign_policy = CASE
        WHEN multiplier = -1 THEN 'flip'
        ELSE 'as_reported'
    END
WHERE aggregation_type IS NULL;

COMMENT ON COLUMN map_concept_to_taxonomy_exception.aggregation_type IS
    'Aggregation role override for this entity exception. Same values as versioned mapping.';
COMMENT ON COLUMN map_concept_to_taxonomy_exception.sign_policy IS
    'Sign handling override for this entity exception.';

-- ---------------------------------------------------------------------------
-- 4. Add suggested aggregation fields to review queue
-- ---------------------------------------------------------------------------

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD COLUMN IF NOT EXISTS suggested_aggregation_type TEXT,
    ADD COLUMN IF NOT EXISTS suggested_sign_policy TEXT,
    ADD COLUMN IF NOT EXISTS suggested_normal_balance TEXT;

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS chk_mctrq_suggested_aggregation_type;
ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT chk_mctrq_suggested_aggregation_type
    CHECK (suggested_aggregation_type IS NULL OR suggested_aggregation_type IN (
        'ROOT',
        'CHILD_SUM',
        'DIRECT',
        'FALLBACK_TOTAL',
        'EXCLUDE'
    ));

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS chk_mctrq_suggested_sign_policy;
ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT chk_mctrq_suggested_sign_policy
    CHECK (suggested_sign_policy IS NULL OR suggested_sign_policy IN (
        'as_reported',
        'flip',
        'force_negative',
        'force_positive'
    ));

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS chk_mctrq_suggested_normal_balance;
ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT chk_mctrq_suggested_normal_balance
    CHECK (suggested_normal_balance IS NULL OR suggested_normal_balance IN (
        'debit',
        'credit'
    ));

COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.suggested_aggregation_type IS
    'AI/deterministic suggested aggregation role for review.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.suggested_sign_policy IS
    'AI/deterministic suggested sign policy for review.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.suggested_normal_balance IS
    'AI/deterministic suggested XBRL normal balance for review.';

-- ---------------------------------------------------------------------------
-- 5. Index for aggregation-type filtering
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_mctv_aggregation_type
    ON map_concept_to_taxonomy_versioned (target_variable, aggregation_type)
    WHERE aggregation_type IS NOT NULL;
