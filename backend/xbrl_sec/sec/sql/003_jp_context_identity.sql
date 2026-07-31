-- Preserve EDINET XBRL context identity in the unified JP raw fact table.

SET search_path TO sec, public;

ALTER TABLE fact_fundamentals_jp ADD COLUMN IF NOT EXISTS context_id TEXT NOT NULL DEFAULT '';
ALTER TABLE fact_fundamentals_jp ADD COLUMN IF NOT EXISTS dimension_signature TEXT NOT NULL DEFAULT '';

UPDATE fact_fundamentals_jp
SET filing_id = ''
WHERE filing_id IS NULL;

ALTER TABLE fact_fundamentals_jp ALTER COLUMN filing_id SET NOT NULL;

ALTER TABLE fact_fundamentals_jp DROP CONSTRAINT IF EXISTS fact_fundamentals_jp_pkey;

DELETE FROM fact_fundamentals_jp f
USING (
    SELECT ctid
    FROM (
        SELECT ctid,
               row_number() OVER (
                   PARTITION BY edinet_code, filing_id, concept_id, period_end,
                                fiscal_period, context_id, value_type
                   ORDER BY updated_at DESC, ctid DESC
               ) AS rn
        FROM fact_fundamentals_jp
    ) ranked
    WHERE rn > 1
) dup
WHERE f.ctid = dup.ctid;

ALTER TABLE fact_fundamentals_jp
    ADD PRIMARY KEY
        (edinet_code, filing_id, concept_id, period_end, fiscal_period, context_id, value_type);

CREATE INDEX IF NOT EXISTS idx_ff_jp_dimension_signature
    ON fact_fundamentals_jp (dimension_signature)
    WHERE dimension_signature <> '';

ALTER TABLE source_filing_state ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'instance';

UPDATE source_filing_state
SET source_kind = CASE
    WHEN jurisdiction = 'JP' AND source_path LIKE '%_xbrl.zip' THEN 'package'
    WHEN jurisdiction = 'JP' THEN 'instance'
    ELSE 'companyfacts'
END;

CREATE INDEX IF NOT EXISTS idx_source_filing_kind
    ON source_filing_state (jurisdiction, source_kind, entity_id);
