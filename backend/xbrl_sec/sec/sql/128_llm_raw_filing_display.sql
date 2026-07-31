-- LLM-curated display layer over persisted filing-native statement rows.
--
-- The LLM writes display instructions only: labels, hierarchy, visibility,
-- aggregation, and source bindings. Numeric values are produced by executing
-- those instructions over DB-stored filing-native rows.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_run (
    llm_display_run_id          BIGSERIAL PRIMARY KEY,
    jurisdiction                TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id                   TEXT NOT NULL,
    ticker                      TEXT,
    filing_id                   TEXT NOT NULL,
    filing_form                 TEXT,
    filed_date                  DATE,
    fiscal_year                 SMALLINT,
    fiscal_period               TEXT,
    period_end                  DATE,
    prompt_version              TEXT NOT NULL,
    schema_version              TEXT NOT NULL,
    model_name                  TEXT NOT NULL,
    input_fingerprint           TEXT NOT NULL,
    source_statement_display_ids BIGINT[] NOT NULL DEFAULT ARRAY[]::BIGINT[],
    status                      TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'failed_validation')),
    error_message               TEXT,
    diagnostics                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_llm_response            JSONB,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at                TIMESTAMPTZ,
    UNIQUE (jurisdiction, entity_id, filing_id, prompt_version, schema_version, input_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_display_run_lookup
    ON fact_llm_raw_filing_display_run (jurisdiction, entity_id, filing_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_statement_display (
    llm_statement_display_id    BIGSERIAL PRIMARY KEY,
    llm_display_run_id          BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_display_run(llm_display_run_id)
                                ON DELETE CASCADE,
    source_statement_display_id BIGINT NOT NULL
                                REFERENCES fact_filing_statement_display(statement_display_id)
                                ON DELETE CASCADE,
    jurisdiction                TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id                   TEXT NOT NULL,
    ticker                      TEXT,
    filing_id                   TEXT NOT NULL,
    filing_form                 TEXT,
    filed_date                  DATE,
    fiscal_year                 SMALLINT,
    fiscal_period               TEXT,
    period_end                  DATE,
    accounting_standard         TEXT NOT NULL,
    api_statement               TEXT NOT NULL CHECK (api_statement IN ('BS', 'IS', 'CF')),
    statement_type              TEXT NOT NULL,
    statement_title             TEXT NOT NULL,
    display_title               TEXT NOT NULL,
    role_uri                    TEXT NOT NULL,
    prompt_version              TEXT NOT NULL,
    schema_version              TEXT NOT NULL,
    model_name                  TEXT NOT NULL,
    input_fingerprint           TEXT NOT NULL,
    status                      TEXT NOT NULL CHECK (status IN ('succeeded', 'failed_validation')),
    diagnostics                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction, entity_id, filing_id, api_statement, prompt_version, schema_version, input_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_statement_lookup
    ON fact_llm_raw_filing_statement_display
        (jurisdiction, ticker, api_statement, fiscal_period, fiscal_year DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_column (
    column_id                   BIGSERIAL PRIMARY KEY,
    llm_statement_display_id    BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_statement_display(llm_statement_display_id)
                                ON DELETE CASCADE,
    column_key                  TEXT NOT NULL,
    label                       TEXT NOT NULL,
    column_kind                 TEXT NOT NULL CHECK (column_kind IN ('instant', 'duration')),
    period_start                DATE,
    period_end                  DATE NOT NULL,
    fiscal_year                 SMALLINT,
    fiscal_period               TEXT,
    column_order                INTEGER NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (llm_statement_display_id, column_key)
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_column_order
    ON fact_llm_raw_filing_display_column (llm_statement_display_id, column_order);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_row (
    row_id                      BIGSERIAL PRIMARY KEY,
    llm_statement_display_id    BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_statement_display(llm_statement_display_id)
                                ON DELETE CASCADE,
    row_key                     TEXT NOT NULL,
    parent_row_key              TEXT,
    display_label               TEXT NOT NULL,
    row_kind                    TEXT NOT NULL CHECK (row_kind IN ('detail', 'subtotal', 'total', 'section')),
    aggregation                 TEXT NOT NULL CHECK (aggregation IN ('direct', 'sum', 'subtract', 'none')),
    visibility                  TEXT NOT NULL CHECK (visibility IN ('default', 'detail', 'hidden')),
    display_depth               SMALLINT NOT NULL DEFAULT 0,
    display_order               INTEGER NOT NULL,
    confidence                  NUMERIC(5, 4),
    rationale                   TEXT,
    raw_spec                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (llm_statement_display_id, row_key)
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_row_order
    ON fact_llm_raw_filing_display_row (llm_statement_display_id, display_order);

CREATE INDEX IF NOT EXISTS idx_llm_raw_row_parent
    ON fact_llm_raw_filing_display_row (llm_statement_display_id, parent_row_key);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_row_source (
    row_id                      BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_display_row(row_id)
                                ON DELETE CASCADE,
    source_node_key             TEXT NOT NULL,
    source_concept_id           TEXT,
    aggregation_weight          NUMERIC NOT NULL DEFAULT 1,
    source_order                INTEGER NOT NULL DEFAULT 0,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (row_id, source_node_key)
);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_value (
    row_id                      BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_display_row(row_id)
                                ON DELETE CASCADE,
    column_key                  TEXT NOT NULL,
    value                       NUMERIC,
    unit                        TEXT,
    provenance                  TEXT NOT NULL CHECK (provenance IN ('direct', 'aggregated', 'section', 'missing_source')),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (row_id, column_key)
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_value_column
    ON fact_llm_raw_filing_display_value (column_key);

CREATE TABLE IF NOT EXISTS fact_llm_raw_filing_display_diagnostic (
    diagnostic_id               BIGSERIAL PRIMARY KEY,
    llm_statement_display_id    BIGINT NOT NULL
                                REFERENCES fact_llm_raw_filing_statement_display(llm_statement_display_id)
                                ON DELETE CASCADE,
    row_key                     TEXT,
    severity                    TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    diagnostic_key              TEXT NOT NULL,
    message                     TEXT NOT NULL,
    details                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_raw_diagnostic_statement
    ON fact_llm_raw_filing_display_diagnostic (llm_statement_display_id, severity);
