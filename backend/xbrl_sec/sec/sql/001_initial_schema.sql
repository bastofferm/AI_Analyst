-- Initial schema for the new MZQA XBRL data layer.
-- Target database: xbrl_sec
-- Target schema: sec

CREATE SCHEMA IF NOT EXISTS sec;
SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS pipeline_stage_run (
    run_id UUID PRIMARY KEY,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP','GLOBAL')),
    stage TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('incremental','reparse','full_refresh','truncate','validate')),
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    rows_in BIGINT NOT NULL DEFAULT 0,
    rows_out BIGINT NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_entity_state (
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    entity_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    source_hash TEXT,
    max_filed_date DATE,
    rows_in BIGINT NOT NULL DEFAULT 0,
    rows_out BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id UUID REFERENCES pipeline_stage_run(run_id),
    PRIMARY KEY (jurisdiction, entity_id, stage)
);

CREATE TABLE IF NOT EXISTS source_filing_state (
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    filing_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    filing_type TEXT,
    filed_date DATE,
    period_end DATE,
    source_hash TEXT,
    downloaded BOOLEAN NOT NULL DEFAULT FALSE,
    extracted BOOLEAN NOT NULL DEFAULT FALSE,
    parsed BOOLEAN NOT NULL DEFAULT FALSE,
    parse_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, filing_id)
);

CREATE TABLE IF NOT EXISTS fact_fundamentals_us (
    cik TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    period_end DATE NOT NULL,
    fiscal_period TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'ORIG' CHECK (value_type IN ('ORIG','REST')),
    filing_id TEXT,
    filing_type TEXT,
    period_start DATE,
    fiscal_year INTEGER,
    source_fp TEXT,
    value NUMERIC,
    unit TEXT,
    filed_date DATE,
    taxonomy TEXT,
    context_tier SMALLINT NOT NULL DEFAULT 0,
    statement_type TEXT,
    parent_id TEXT,
    root_id TEXT,
    concept_path TEXT,
    concept_id_level SMALLINT,
    weight NUMERIC,
    effective_weight NUMERIC,
    pre_parent_id TEXT,
    pre_order INTEGER,
    pre_level SMALLINT,
    pre_position INTEGER,
    restatement_counter INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, concept_id, period_end, fiscal_period, context_tier, value_type)
);

CREATE TABLE IF NOT EXISTS fact_fundamentals_jp (
    edinet_code TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    period_end DATE NOT NULL,
    fiscal_period TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'ORIG' CHECK (value_type IN ('ORIG','REST')),
    filing_id TEXT,
    filing_type TEXT,
    period_start DATE,
    fiscal_year INTEGER,
    source_fp TEXT,
    value NUMERIC,
    unit TEXT,
    decimals SMALLINT,
    filed_date DATE,
    taxonomy TEXT,
    context_tier SMALLINT NOT NULL DEFAULT 0,
    statement_type TEXT,
    parent_id TEXT,
    root_id TEXT,
    concept_path TEXT,
    concept_id_level SMALLINT,
    weight NUMERIC,
    effective_weight NUMERIC,
    pre_parent_id TEXT,
    pre_order INTEGER,
    pre_level SMALLINT,
    pre_position INTEGER,
    restatement_counter INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (edinet_code, concept_id, period_end, fiscal_period, context_tier, value_type)
);

CREATE INDEX IF NOT EXISTS idx_ff_us_entity_year ON fact_fundamentals_us (cik, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_ff_us_concept ON fact_fundamentals_us (concept_id);
CREATE INDEX IF NOT EXISTS idx_ff_us_filing ON fact_fundamentals_us (filing_id);
CREATE INDEX IF NOT EXISTS idx_ff_jp_entity_year ON fact_fundamentals_jp (edinet_code, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_ff_jp_concept ON fact_fundamentals_jp (concept_id);
CREATE INDEX IF NOT EXISTS idx_ff_jp_filing ON fact_fundamentals_jp (filing_id);
