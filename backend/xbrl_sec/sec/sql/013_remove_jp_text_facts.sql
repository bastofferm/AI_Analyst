-- fact_fundamentals_jp is numeric fundamentals only.
-- Textual issuer identity is kept on dim_company_jp and source_filing_state.raw_payload.

SET search_path TO sec, public;

DELETE FROM fact_fundamentals_jp
WHERE value IS NULL;

DROP INDEX IF EXISTS idx_ff_jp_text_concept;
ALTER TABLE fact_fundamentals_jp DROP COLUMN IF EXISTS value_text;
