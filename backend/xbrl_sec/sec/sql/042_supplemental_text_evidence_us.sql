-- Supplemental filing-text evidence for strict XBRL gaps.
-- This layer is intentionally separate from standardized fundamentals.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_fundamentals_text_evidence_us (
    cik TEXT NOT NULL,
    ticker TEXT,
    filing_id TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL DEFAULT 'FY',
    period_end DATE,
    line_item_id TEXT NOT NULL REFERENCES ref_standardized_line_items(line_item_id),
    value NUMERIC,
    currency TEXT,
    source_label TEXT,
    source_excerpt TEXT,
    source_path TEXT,
    extraction_method TEXT NOT NULL
        CHECK (extraction_method IN ('html_table_regex','mda_table_regex')),
    confidence NUMERIC NOT NULL
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        cik, filing_id, fiscal_year, fiscal_period,
        line_item_id, extraction_method, source_label
    )
);

CREATE INDEX IF NOT EXISTS idx_fact_fundamentals_text_evidence_us_ticker_year
    ON fact_fundamentals_text_evidence_us (ticker, fiscal_year DESC, fiscal_period);

CREATE INDEX IF NOT EXISTS idx_fact_fundamentals_text_evidence_us_line_item
    ON fact_fundamentals_text_evidence_us (line_item_id, fiscal_year DESC);

COMMENT ON TABLE fact_fundamentals_text_evidence_us IS
    'Lower-confidence filing-text evidence for component values not tagged as strict XBRL facts. Never feeds fact_fundamentals_std_us by default.';

CREATE TABLE IF NOT EXISTS fact_fundamentals_quality_us (
    cik TEXT NOT NULL,
    ticker TEXT,
    filing_id TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL DEFAULT 'FY',
    period_end DATE,
    quality_code TEXT NOT NULL,
    line_item_id TEXT NOT NULL REFERENCES ref_standardized_line_items(line_item_id),
    severity TEXT NOT NULL DEFAULT 'WARN'
        CHECK (severity IN ('INFO','WARN','ERROR')),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','RESOLVED','WAIVED')),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (
        cik, filing_id, fiscal_year, fiscal_period,
        quality_code, line_item_id
    )
);

CREATE INDEX IF NOT EXISTS idx_fact_fundamentals_quality_us_ticker_year
    ON fact_fundamentals_quality_us (ticker, fiscal_year DESC, fiscal_period);

CREATE INDEX IF NOT EXISTS idx_fact_fundamentals_quality_us_code
    ON fact_fundamentals_quality_us (quality_code, fiscal_year DESC);

COMMENT ON TABLE fact_fundamentals_quality_us IS
    'Filing-level fundamental data-quality flags. This is an audit/display surface, not a governed mapping table.';

CREATE OR REPLACE VIEW vw_us_nonoperating_component_quality AS
SELECT q.cik,
       q.ticker,
       q.filing_id,
       q.fiscal_year,
       q.fiscal_period,
       q.period_end,
       q.line_item_id,
       q.quality_code,
       q.severity,
       q.status,
       q.details,
       e.value AS supplemental_value,
       e.currency AS supplemental_currency,
       e.source_label AS supplemental_source_label,
       e.extraction_method AS supplemental_extraction_method,
       e.confidence AS supplemental_confidence
FROM fact_fundamentals_quality_us q
LEFT JOIN fact_fundamentals_text_evidence_us e
  ON e.cik = q.cik
 AND e.filing_id = q.filing_id
 AND e.fiscal_year = q.fiscal_year
 AND e.fiscal_period = q.fiscal_period
 AND e.line_item_id = q.line_item_id
WHERE q.quality_code = 'aggregate_only_nonoperating_detail_missing';

CREATE TABLE IF NOT EXISTS fact_metrics_supplemental_us (
    cik TEXT NOT NULL,
    ticker TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL DEFAULT 'FY',
    period_end DATE,
    metric_id TEXT NOT NULL,
    value NUMERIC,
    unit_type TEXT,
    source_quality TEXT NOT NULL DEFAULT 'SUPPLEMENTAL_TEXT',
    source_line_item_id TEXT,
    source_filing_id TEXT,
    formula_with_values TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, fiscal_year, fiscal_period, metric_id, source_quality)
);

CREATE INDEX IF NOT EXISTS idx_fact_metrics_supplemental_us_ticker_year
    ON fact_metrics_supplemental_us (ticker, fiscal_year DESC, fiscal_period);

COMMENT ON TABLE fact_metrics_supplemental_us IS
    'Optional analytics derived from lower-confidence supplemental text evidence. Kept separate from strict fact_metrics_us.';

INSERT INTO ref_metric_definitions
    (metric_id, category, name, importance, formula, required_line_items,
     note, unit_type, metric_type, formula_symbolic, sector_scope,
     interpretation, registry_source, updated_at)
VALUES
    ('interest_coverage_supplemental_text', 'solvency_liquidity',
     'Interest Coverage (Supplemental Text)', 920,
     'EBIT / abs(text-extracted interest expense)',
     ARRAY['earnings_before_interest_taxes','interest_expense'],
     'Lower-confidence display-only metric computed only when interest expense was not XBRL-tagged but explicit filing-text evidence exists.',
     'x', 'SUPPLEMENTAL_TEXT',
     '\\mathrm{Interest\\ Coverage}_{text}=\\frac{EBIT}{|Interest\\ Expense_{text}|}',
     'universal',
     'Use only as a disclosure aid. It is not part of the strict metrics table.',
     '042_supplemental_text_evidence_us.sql', now())
ON CONFLICT (metric_id) DO UPDATE SET
    category = EXCLUDED.category,
    name = EXCLUDED.name,
    importance = EXCLUDED.importance,
    formula = EXCLUDED.formula,
    required_line_items = EXCLUDED.required_line_items,
    note = EXCLUDED.note,
    unit_type = EXCLUDED.unit_type,
    metric_type = EXCLUDED.metric_type,
    formula_symbolic = EXCLUDED.formula_symbolic,
    sector_scope = EXCLUDED.sector_scope,
    interpretation = EXCLUDED.interpretation,
    registry_source = EXCLUDED.registry_source,
    updated_at = now();
