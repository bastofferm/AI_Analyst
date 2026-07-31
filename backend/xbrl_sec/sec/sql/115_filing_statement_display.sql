-- Filing-native financial statement display projection.
--
-- These tables store a presentation-linkbase-shaped display layer after
-- ingestion/backfill. Runtime APIs should read these tables, not local filing
-- HTML or XBRL files.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_filing_statement_display (
    statement_display_id           BIGSERIAL PRIMARY KEY,
    jurisdiction                   TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id                      TEXT NOT NULL,
    ticker                         TEXT,
    filing_id                      TEXT NOT NULL,
    filing_form                    TEXT,
    filed_date                     DATE,
    fiscal_year                    SMALLINT,
    fiscal_period                  TEXT,
    period_end                     DATE,
    accounting_standard            TEXT NOT NULL DEFAULT 'US_GAAP',
    api_statement                  TEXT NOT NULL CHECK (api_statement IN ('BS', 'IS', 'CF')),
    statement_type                 TEXT NOT NULL,
    statement_title                TEXT NOT NULL,
    standardized_statement_label   TEXT NOT NULL,
    role_uri                       TEXT NOT NULL,
    source_path                    TEXT,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction, entity_id, filing_id, statement_type, role_uri)
);

COMMENT ON TABLE fact_filing_statement_display IS
    'One filing-native rendered statement per entity filing and presentation role.';

CREATE TABLE IF NOT EXISTS fact_filing_statement_display_column (
    column_id              BIGSERIAL PRIMARY KEY,
    statement_display_id   BIGINT NOT NULL
                            REFERENCES fact_filing_statement_display(statement_display_id)
                            ON DELETE CASCADE,
    column_key             TEXT NOT NULL,
    label                  TEXT NOT NULL,
    column_kind            TEXT NOT NULL CHECK (column_kind IN ('instant', 'duration')),
    period_start           DATE,
    period_end             DATE NOT NULL,
    fiscal_year            SMALLINT,
    fiscal_period          TEXT,
    column_order           INTEGER NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (statement_display_id, column_key)
);

CREATE INDEX IF NOT EXISTS idx_ffsd_column_statement_order
    ON fact_filing_statement_display_column (statement_display_id, column_order);

CREATE TABLE IF NOT EXISTS fact_filing_statement_display_node (
    node_id                    BIGSERIAL PRIMARY KEY,
    statement_display_id       BIGINT NOT NULL
                                REFERENCES fact_filing_statement_display(statement_display_id)
                                ON DELETE CASCADE,
    node_key                   TEXT NOT NULL,
    parent_node_key            TEXT,
    source_concept_id          TEXT NOT NULL,
    source_parent_concept_id   TEXT,
    value_binding_concept_id   TEXT,
    std_line_item_id           TEXT,
    raw_label                  TEXT,
    standardized_label         TEXT,
    display_label              TEXT NOT NULL,
    display_role               TEXT NOT NULL,
    default_visibility         TEXT NOT NULL CHECK (default_visibility IN ('default', 'detail', 'hidden')),
    is_abstract                BOOLEAN NOT NULL DEFAULT FALSE,
    presentation_depth         SMALLINT NOT NULL DEFAULT 0,
    display_depth              SMALLINT NOT NULL DEFAULT 0,
    display_order              INTEGER NOT NULL,
    source_role_uri            TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (statement_display_id, node_key)
);

CREATE INDEX IF NOT EXISTS idx_ffsd_node_statement_order
    ON fact_filing_statement_display_node (statement_display_id, display_order);

CREATE INDEX IF NOT EXISTS idx_ffsd_node_statement_parent
    ON fact_filing_statement_display_node (statement_display_id, parent_node_key);

CREATE TABLE IF NOT EXISTS fact_filing_statement_display_value (
    node_id             BIGINT NOT NULL
                        REFERENCES fact_filing_statement_display_node(node_id)
                        ON DELETE CASCADE,
    column_key          TEXT NOT NULL,
    value               NUMERIC,
    unit                TEXT,
    source_concept_id   TEXT,
    source_fact_id      TEXT,
    provenance          TEXT NOT NULL CHECK (provenance IN ('inline_xbrl', 'raw_fact', 'bound_total')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, column_key)
);

CREATE INDEX IF NOT EXISTS idx_ffsd_value_column
    ON fact_filing_statement_display_value (column_key);
