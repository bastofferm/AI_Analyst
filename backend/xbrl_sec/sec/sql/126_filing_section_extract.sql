-- 126_filing_section_extract.sql
-- LLM-extracted structured filing sections (Item 1A Risk, Item 7 MD&A, Item 9A Controls).
-- Written by the sec_text_extract LangGraph subgraph; opt-in via extract_sections=True.

CREATE TABLE IF NOT EXISTS sec.filing_section_extract (
    extract_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id       TEXT NOT NULL,
    cik             TEXT,
    accession       TEXT,
    item            TEXT NOT NULL,
    text_excerpt    TEXT,
    summary         TEXT,
    key_risks       JSONB NOT NULL DEFAULT '[]'::jsonb,
    sentiment       TEXT,
    model_version   TEXT,
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (filing_id, item, model_version)
);

CREATE INDEX IF NOT EXISTS idx_filing_section_extract_filing
    ON sec.filing_section_extract(filing_id, item);

CREATE INDEX IF NOT EXISTS idx_filing_section_extract_cik
    ON sec.filing_section_extract(cik, item, extracted_at DESC);

COMMENT ON TABLE sec.filing_section_extract IS
  'LLM-extracted filing sections paired with structured metadata. Combined with '
  'XBRL facts this enables semantic search across risk factors and MD&A.';
