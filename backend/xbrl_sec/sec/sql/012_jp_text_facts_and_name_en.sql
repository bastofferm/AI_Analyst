-- Preserve scalar text facts from JP XBRL and expose English issuer names in the JP master.

SET search_path TO sec, public;

ALTER TABLE dim_company_jp ADD COLUMN IF NOT EXISTS name_en TEXT;
ALTER TABLE fact_fundamentals_jp ADD COLUMN IF NOT EXISTS value_text TEXT;

CREATE INDEX IF NOT EXISTS idx_ff_jp_text_concept
    ON fact_fundamentals_jp (concept_id)
    WHERE value_text IS NOT NULL;
