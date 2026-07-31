-- 132_rich_filing_sections_us.sql
-- Deterministic iXBRL TextBlock disclosures used as qualitative committee evidence.
-- These rows do not change standardized financial metrics or governed mappings.

CREATE TABLE IF NOT EXISTS sec.fact_rich_filing_sections_us (
    rich_section_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cik                   TEXT NOT NULL,
    ticker                TEXT,
    accession_no          TEXT NOT NULL,
    filing_id             TEXT NOT NULL,
    form_type             TEXT,
    filing_date           DATE,
    fiscal_year           INTEGER,
    fiscal_period         TEXT,
    concept_name          TEXT NOT NULL,
    section_family        TEXT NOT NULL,
    sector_scope          TEXT,
    section_title         TEXT,
    plain_text            TEXT,
    tables_jsonb          JSONB NOT NULL DEFAULT '[]'::jsonb,
    metrics_preview_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_html_path      TEXT,
    source_anchor         TEXT,
    text_hash             TEXT NOT NULL,
    quality_score         NUMERIC(8,2) NOT NULL DEFAULT 0,
    extraction_version    TEXT NOT NULL DEFAULT 'rich-xbrl-textblock-v1',
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cik, accession_no, concept_name, text_hash, extraction_version)
);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_cik_date
    ON sec.fact_rich_filing_sections_us(cik, filing_date DESC, quality_score DESC);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_ticker_date
    ON sec.fact_rich_filing_sections_us(UPPER(ticker), filing_date DESC, quality_score DESC);

CREATE INDEX IF NOT EXISTS idx_rich_filing_sections_us_family
    ON sec.fact_rich_filing_sections_us(section_family, sector_scope, quality_score DESC);

COMMENT ON TABLE sec.fact_rich_filing_sections_us IS
  'Ranked iXBRL TextBlock sections with embedded tables for committee evidence. '
  'Advisory only: canonical valuation metrics continue to come from standardized facts.';
