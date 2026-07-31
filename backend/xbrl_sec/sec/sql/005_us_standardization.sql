-- US standardization layer for the MZQA xbrl_sec.sec pipeline.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_fundamentals_std_us (
    cik TEXT NOT NULL,
    jurisdiction TEXT NOT NULL DEFAULT 'US',
    fiscal_year SMALLINT NOT NULL,
    fiscal_period TEXT NOT NULL,
    period_end DATE,
    line_item_id TEXT NOT NULL,
    metric_type VARCHAR(16) NOT NULL DEFAULT 'RAW',
    value NUMERIC,
    currency TEXT,
    source_concept_id TEXT,
    filing_form TEXT,
    filed_date DATE,
    filing_id TEXT,
    concept_path TEXT,
    std_concept_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, jurisdiction, fiscal_year, fiscal_period, line_item_id)
);

CREATE INDEX IF NOT EXISTS idx_ffstd_us_line_item
    ON fact_fundamentals_std_us (line_item_id);

CREATE INDEX IF NOT EXISTS idx_ffstd_us_period
    ON fact_fundamentals_std_us (cik, fiscal_year, fiscal_period);
