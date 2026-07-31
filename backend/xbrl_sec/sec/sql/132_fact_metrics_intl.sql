-- International (Yahoo-backed) fundamentals metric layer.
--
-- Mirrors fact_metrics_us / fact_metrics_jp so the AI screener and downstream
-- committee code can use one uniform metric-layer shape across all three
-- jurisdictions. The only structural difference is the entity key:
--   US:   cik              (10-digit zero-padded)
--   JP:   edinet_code
--   INTL: intl_company_id  (TEXT surrogate from dim_company_intl)
--
-- Rows here are ALWAYS derived from Yahoo (yfinance) statement / profile data —
-- never from XBRL. trace_quality on the sibling fact_metrics_recon_intl (below)
-- is always 'computed_only' so downstream DQ code recognizes that no XBRL
-- source-concept trace exists.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_metrics_intl (
    ticker           TEXT NOT NULL,
    intl_company_id  TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    primary_id       TEXT,
    primary_id_type  TEXT,
    jurisdiction     TEXT NOT NULL DEFAULT 'INTL',
    fiscal_year      INTEGER NOT NULL,
    fiscal_period    TEXT NOT NULL,
    period_end       DATE,
    metric_id        TEXT NOT NULL,
    formula          TEXT,
    metric_type      TEXT,
    category         TEXT,
    importance       INTEGER,
    unit_type        TEXT,
    value            DOUBLE PRECISION,
    currency         TEXT,
    fallback_applied BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, intl_company_id, fiscal_year, fiscal_period, metric_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_metrics_intl_ticker_period
    ON fact_metrics_intl (ticker, fiscal_period, fiscal_year DESC);

CREATE INDEX IF NOT EXISTS idx_fact_metrics_intl_metric
    ON fact_metrics_intl (metric_id, fiscal_period, fiscal_year DESC);

COMMENT ON TABLE fact_metrics_intl IS
    'International (Yahoo-backed) fundamentals metric layer, mirroring fact_metrics_us/_jp. Rows are always Yahoo-derived; no XBRL trace exists.';

-- Sibling recon table (kept for shape parity with US/JP, though we always write
-- trace_quality='computed_only' since Yahoo cannot provide XBRL source concepts).
CREATE TABLE IF NOT EXISTS fact_metrics_recon_intl (
    ticker              TEXT NOT NULL,
    intl_company_id     TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    fiscal_year         INTEGER NOT NULL,
    fiscal_period       TEXT NOT NULL,
    period_end          DATE,
    metric_id           TEXT NOT NULL,
    formula             TEXT,
    formula_with_values TEXT,
    value               DOUBLE PRECISION,
    currency            TEXT,
    metric_type         TEXT,
    category            TEXT,
    importance          INTEGER,
    unit_type           TEXT,
    fallback_applied    BOOLEAN NOT NULL DEFAULT FALSE,
    input_values        JSONB,
    source_line_items   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source_concept_ids  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source_filing_ids   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    raw_trace           JSONB,
    trace_quality       TEXT NOT NULL DEFAULT 'computed_only',
    computed_at         TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, intl_company_id, fiscal_year, fiscal_period, metric_id)
);

COMMENT ON TABLE fact_metrics_recon_intl IS
    'Recon trace for INTL metrics. trace_quality is always computed_only because Yahoo does not expose XBRL source concepts.';
