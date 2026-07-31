-- Flag entities that cannot be backfilled from SEC XBRL data and exclude
-- them from include_in_pipeline so coverage gap metrics stop counting them.
--
-- Background: of the 1,253 US entities marked include_in_pipeline that had
-- no rows in fact_fundamentals_us, 1,197 were FPI/cross-border filers
-- (20-F/40-F) that the annual_10k_current_only=TRUE filter was dropping
-- (fixed by the in-flight 10-Q ingestion run). The remaining 56 had no
-- source_filing_state row at all, and a discovery sync_companyfacts_index
-- pass discovered 0 new filings — either because:
--   * SEC has no companyfacts JSON file on disk for them (closed-end funds
--     that file N-CSR, foreign listings without us-gaap XBRL, delisted
--     shells), OR
--   * the JSON file exists but contains zero core XBRL facts.
--
-- This migration:
--   1. Adds a documented pipeline_exclusion_reason column to dim_company_us
--      so future audits know why an entity is excluded.
--   2. Flags the 56 unbackfillable CIKs with a per-entity reason and sets
--      include_in_pipeline = FALSE.

SET search_path TO sec, public;

ALTER TABLE dim_company_us
    ADD COLUMN IF NOT EXISTS pipeline_exclusion_reason TEXT;

ALTER TABLE dim_company_us
    DROP CONSTRAINT IF EXISTS chk_dim_company_us_exclusion_reason;
ALTER TABLE dim_company_us
    ADD CONSTRAINT chk_dim_company_us_exclusion_reason
    CHECK (
        pipeline_exclusion_reason IS NULL OR pipeline_exclusion_reason IN (
            'likely_closed_end_fund',
            'gics_financial_no_xbrl',
            'unclassified_no_xbrl',
            'no_sec_companyfacts_data',
            'manual_exclusion'
        )
    );

COMMENT ON COLUMN dim_company_us.pipeline_exclusion_reason IS
    'When include_in_pipeline = FALSE and this entity is intentionally out of scope, this column documents why. NULL means the entity is in scope or was excluded by an old process before this column existed.';

-- Categorize and flag the 56 unbackfillable CIKs. Heuristic:
--   * GICS sector 40 (Financials) + 3-char NYSE ticker → likely CEF (file N-CSR, not 10-K)
--   * GICS sector 40 + other ticker length → financial without XBRL (probably CEF too, or a CIK error)
--   * No GICS sector code → unclassified (likely delisted shell or stale dim row)
--   * Any other GICS sector with no SEC data → no SEC companyfacts data (genuine miss)

WITH unbackfillable AS (
    SELECT
        cik,
        CASE
            WHEN gics_sector_code = '40' AND length(primary_ticker) = 3
                THEN 'likely_closed_end_fund'
            WHEN gics_sector_code = '40'
                THEN 'gics_financial_no_xbrl'
            WHEN gics_sector_code IS NULL
                THEN 'unclassified_no_xbrl'
            ELSE
                'no_sec_companyfacts_data'
        END AS reason
    FROM dim_company_us d
    WHERE COALESCE(d.include_in_pipeline, FALSE)
      AND NOT EXISTS (SELECT 1 FROM fact_fundamentals_us f WHERE f.cik = d.cik)
      AND NOT EXISTS (SELECT 1 FROM source_filing_state s WHERE s.entity_id = d.cik AND s.jurisdiction = 'US')
)
UPDATE dim_company_us d
SET
    include_in_pipeline = FALSE,
    pipeline_exclusion_reason = u.reason
FROM unbackfillable u
WHERE d.cik = u.cik;
