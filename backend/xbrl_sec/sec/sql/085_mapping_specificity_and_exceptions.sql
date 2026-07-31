-- Governed mapping specificity, company-period exceptions, and mapped anomaly review.
--
-- This extends the mapping layer without changing raw facts or automatically
-- promoting any review queue suggestions into production mappings.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Review queue support for already-mapped anomalies
-- ---------------------------------------------------------------------------

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD COLUMN IF NOT EXISTS current_mapping_id BIGINT,
    ADD COLUMN IF NOT EXISTS proposed_action TEXT,
    ADD COLUMN IF NOT EXISTS exception_entity_id TEXT,
    ADD COLUMN IF NOT EXISTS exception_fiscal_year_from SMALLINT,
    ADD COLUMN IF NOT EXISTS exception_fiscal_year_to SMALLINT,
    ADD COLUMN IF NOT EXISTS exception_fiscal_period TEXT;

ALTER TABLE map_concept_to_taxonomy_review_queue
    DROP CONSTRAINT IF EXISTS map_concept_to_taxonomy_review_queue_review_class_check;

ALTER TABLE map_concept_to_taxonomy_review_queue
    ADD CONSTRAINT map_concept_to_taxonomy_review_queue_review_class_check
    CHECK (review_class IN (
        'map_candidate',
        'special_case_review',
        'likely_exclude',
        'mapped_anomaly',
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
            'supplemental_only'
        )
    );

CREATE INDEX IF NOT EXISTS idx_mctrq_current_mapping
    ON map_concept_to_taxonomy_review_queue (jurisdiction, current_mapping_id)
    WHERE current_mapping_id IS NOT NULL;

COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.review_class IS
    'Review lane. mapped_anomaly rows are generated from already-standardized facts with identity, sector, accounting-standard, taxonomy, residual, or display issues.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.current_mapping_id IS
    'Current production mapping implicated by a mapped_anomaly review packet.';
COMMENT ON COLUMN map_concept_to_taxonomy_review_queue.proposed_action IS
    'Advisory action only: keep, scope/split/sign-fix, company exception, unmap, or supplemental-only.';

-- ---------------------------------------------------------------------------
-- Narrow company/entity exception layer
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS map_concept_to_taxonomy_exception (
    exception_id BIGSERIAL PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    fiscal_year_from SMALLINT NOT NULL,
    fiscal_year_to SMALLINT,
    fiscal_period TEXT,
    target_variable TEXT NOT NULL,
    tier INTEGER NOT NULL,
    multiplier NUMERIC NOT NULL DEFAULT 1 CHECK (multiplier IN (-1, 1)),
    mapping_sector TEXT NOT NULL DEFAULT '',
    accounting_standard TEXT,
    taxonomy_version TEXT,
    reason_code TEXT NOT NULL,
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (review_status IN ('draft', 'queued', 'approved', 'rejected', 'expired')),
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    reevaluate_after DATE,
    current_mapping_id BIGINT,
    suggestion_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (fiscal_year_to IS NULL OR fiscal_year_from <= fiscal_year_to),
    CHECK (
        review_status <> 'approved'
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);

COMMENT ON TABLE map_concept_to_taxonomy_exception IS
    'Governed last-resort entity/concept/year exception layer. Exceptions are narrow, review-gated, auditable, and expire or require reevaluation.';
COMMENT ON COLUMN map_concept_to_taxonomy_exception.evidence_json IS
    'Evidence packet with labels, XBRL presentation/calculation context, units, values, identity deltas, affected years, and rationale.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_mct_exception_approved_scope
    ON map_concept_to_taxonomy_exception (
        jurisdiction,
        entity_id,
        concept_id,
        fiscal_year_from,
        COALESCE(fiscal_year_to, 9999),
        COALESCE(fiscal_period, '')
    )
    WHERE review_status = 'approved';

CREATE INDEX IF NOT EXISTS idx_mct_exception_lookup
    ON map_concept_to_taxonomy_exception (
        jurisdiction,
        entity_id,
        concept_id,
        fiscal_year_from,
        COALESCE(fiscal_year_to, 9999),
        COALESCE(fiscal_period, ''),
        review_status
    );

CREATE INDEX IF NOT EXISTS idx_mct_exception_review
    ON map_concept_to_taxonomy_exception (jurisdiction, review_status, reevaluate_after);

CREATE OR REPLACE FUNCTION touch_mapping_exception_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mct_exception_touch_updated_at ON map_concept_to_taxonomy_exception;
CREATE TRIGGER trg_mct_exception_touch_updated_at
BEFORE UPDATE ON map_concept_to_taxonomy_exception
FOR EACH ROW
EXECUTE FUNCTION touch_mapping_exception_updated_at();

-- ---------------------------------------------------------------------------
-- Selection and audit metadata
-- ---------------------------------------------------------------------------

ALTER TABLE fact_fundamentals_std_us
    ADD COLUMN IF NOT EXISTS mapping_exception_id BIGINT;

ALTER TABLE fact_fundamentals_std_jp
    ADD COLUMN IF NOT EXISTS mapping_exception_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_ffstd_us_mapping_exception_id
    ON fact_fundamentals_std_us (mapping_exception_id)
    WHERE mapping_exception_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ffstd_jp_mapping_exception_id
    ON fact_fundamentals_std_jp (mapping_exception_id)
    WHERE mapping_exception_id IS NOT NULL;

COMMENT ON COLUMN fact_fundamentals_std_us.mapping_exception_id IS
    'Approved company-period mapping exception used for this standardized fact, when any.';
COMMENT ON COLUMN fact_fundamentals_std_jp.mapping_exception_id IS
    'Approved company-period mapping exception used for this standardized fact, when any.';

-- Treat accounting standard and taxonomy version as part of mapping specificity.
CREATE OR REPLACE FUNCTION prevent_versioned_mapping_overlap()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    conflicting_mapping_id BIGINT;
BEGIN
    SELECT m.mapping_id
      INTO conflicting_mapping_id
      FROM map_concept_to_taxonomy_versioned m
     WHERE m.mapping_id <> COALESCE(NEW.mapping_id, -1)
       AND (m.jurisdiction = NEW.jurisdiction
            OR m.jurisdiction = 'BOTH'
            OR NEW.jurisdiction = 'BOTH')
       AND m.concept_id = NEW.concept_id
       AND m.mapping_sector IS NOT DISTINCT FROM NEW.mapping_sector
       AND m.accounting_standard IS NOT DISTINCT FROM NEW.accounting_standard
       AND m.taxonomy_version IS NOT DISTINCT FROM NEW.taxonomy_version
       AND m.gics_sector IS NOT DISTINCT FROM NEW.gics_sector
       AND m.gics_industry_group IS NOT DISTINCT FROM NEW.gics_industry_group
       AND m.gics_industry IS NOT DISTINCT FROM NEW.gics_industry
       AND m.gics_sub_industry IS NOT DISTINCT FROM NEW.gics_sub_industry
       AND m.effective_from_year <= COALESCE(NEW.effective_to_year, 9999)
       AND NEW.effective_from_year <= COALESCE(m.effective_to_year, 9999)
     LIMIT 1;

    IF conflicting_mapping_id IS NOT NULL THEN
        RAISE EXCEPTION
            'Overlapping versioned concept mapping % conflicts with concept %, jurisdiction %, sector %, accounting standard %, taxonomy %, years %-%',
            conflicting_mapping_id,
            NEW.concept_id,
            NEW.jurisdiction,
            NEW.mapping_sector,
            COALESCE(NEW.accounting_standard, ''),
            COALESCE(NEW.taxonomy_version, ''),
            NEW.effective_from_year,
            COALESCE(NEW.effective_to_year::TEXT, 'open');
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
