SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS stage_stock_splits (
    run_id        UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    source        TEXT NOT NULL DEFAULT 'stock_splits',
    source_key    TEXT NOT NULL,
    jurisdiction  TEXT NOT NULL,
    entity_id     TEXT,
    ticker        TEXT NOT NULL,
    event_date    DATE,
    effective_date DATE NOT NULL,
    split_ratio   NUMERIC NOT NULL,
    source_type   TEXT NOT NULL,
    source_filing_id TEXT,
    confidence    NUMERIC,
    notes         TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, jurisdiction, ticker, effective_date, source_type)
);

CREATE INDEX IF NOT EXISTS idx_stage_stock_splits_source_key
    ON stage_stock_splits (source, source_key);

CREATE TABLE IF NOT EXISTS fact_stock_split_event (
    jurisdiction     TEXT NOT NULL,
    entity_id        TEXT,
    ticker           TEXT NOT NULL,
    event_date       DATE,
    effective_date   DATE NOT NULL,
    split_ratio      NUMERIC NOT NULL CHECK (split_ratio > 0),
    source_type      TEXT NOT NULL CHECK (source_type IN ('SEC_8K','YFINANCE','QUANT_DETECTED','MANUAL')),
    source_filing_id TEXT,
    confidence       NUMERIC CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, ticker, effective_date, source_type)
);

CREATE INDEX IF NOT EXISTS idx_fact_stock_split_event_entity
    ON fact_stock_split_event (jurisdiction, entity_id, effective_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_stock_split_event_ticker
    ON fact_stock_split_event (jurisdiction, ticker, effective_date DESC);

COMMENT ON TABLE stage_stock_splits IS
    'Run-scoped staging rows for stock split downloads before merging into fact_stock_split_event.';

COMMENT ON TABLE fact_stock_split_event IS
    'Stock split and reverse split events used to split-adjust per-share and share-count metrics without changing raw XBRL facts.';
