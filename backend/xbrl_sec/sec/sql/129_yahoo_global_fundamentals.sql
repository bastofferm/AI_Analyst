SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_company_intl (
    intl_company_id              TEXT PRIMARY KEY,
    name                         TEXT,
    name_en                      TEXT,
    primary_ticker               TEXT,
    exchange                     TEXT,
    exchange_suffix              TEXT,
    region                       TEXT,
    country_code                 TEXT,
    country_name                 TEXT,
    currency                     TEXT,
    quote_type                   TEXT,
    isin                         TEXT,
    lei                          TEXT,
    website                      TEXT,
    sector                       TEXT,
    industry                     TEXT,
    gics_sector_code             TEXT,
    gics_sector_name             TEXT,
    gics_industry_group_code     TEXT,
    gics_industry_group_name     TEXT,
    mapping_sector               TEXT,
    market_cap                   NUMERIC,
    shares_outstanding           NUMERIC,
    is_active                    BOOLEAN NOT NULL DEFAULT TRUE,
    include_in_pipeline          BOOLEAN NOT NULL DEFAULT TRUE,
    pipeline_sample_group        TEXT,
    source                       TEXT NOT NULL DEFAULT 'yahoo_finance',
    raw_profile                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_company_intl_primary_ticker
    ON dim_company_intl (primary_ticker)
    WHERE primary_ticker IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dim_company_intl_country
    ON dim_company_intl (country_code);

CREATE INDEX IF NOT EXISTS idx_dim_company_intl_region
    ON dim_company_intl (region);

CREATE INDEX IF NOT EXISTS idx_dim_company_intl_exchange
    ON dim_company_intl (exchange);

CREATE INDEX IF NOT EXISTS idx_dim_company_intl_mapping_sector
    ON dim_company_intl (mapping_sector);

CREATE INDEX IF NOT EXISTS idx_dim_company_intl_pipeline_scope
    ON dim_company_intl (include_in_pipeline, pipeline_sample_group, intl_company_id);

CREATE TABLE IF NOT EXISTS ref_yahoo_index (
    index_code       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    region           TEXT NOT NULL,
    country_code     TEXT,
    country_name     TEXT,
    default_suffix   TEXT,
    wikipedia_url    TEXT,
    yahoo_symbol     TEXT,
    source           TEXT NOT NULL DEFAULT 'wikipedia',
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ref_yahoo_index_region
    ON ref_yahoo_index (region, is_active);

CREATE TABLE IF NOT EXISTS ref_yahoo_index_constituent (
    index_code        TEXT NOT NULL REFERENCES ref_yahoo_index(index_code) ON DELETE CASCADE,
    intl_company_id   TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    primary_ticker    TEXT NOT NULL,
    constituent_name  TEXT,
    country_code      TEXT,
    exchange_suffix   TEXT,
    source_name       TEXT NOT NULL,
    source_url        TEXT,
    source_rank       INTEGER,
    raw_payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (index_code, intl_company_id)
);

CREATE INDEX IF NOT EXISTS idx_ref_yahoo_index_constituent_company
    ON ref_yahoo_index_constituent (intl_company_id, is_active);

CREATE INDEX IF NOT EXISTS idx_ref_yahoo_index_constituent_ticker
    ON ref_yahoo_index_constituent (primary_ticker);

CREATE TABLE IF NOT EXISTS fact_yahoo_fundamental_metric (
    intl_company_id  TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    as_of_date       DATE NOT NULL DEFAULT CURRENT_DATE,
    metric_id        TEXT NOT NULL,
    value            NUMERIC,
    value_text       TEXT,
    currency         TEXT,
    source           TEXT NOT NULL DEFAULT 'yahoo_finance',
    raw_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (intl_company_id, as_of_date, metric_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_yahoo_fundamental_metric_metric
    ON fact_yahoo_fundamental_metric (metric_id, as_of_date DESC);

CREATE TABLE IF NOT EXISTS fact_yahoo_statement_item (
    intl_company_id  TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    statement_type   TEXT NOT NULL,
    period_type      TEXT NOT NULL DEFAULT 'annual',
    period_end       DATE NOT NULL,
    fiscal_year      INTEGER,
    line_item        TEXT NOT NULL,
    value            NUMERIC,
    currency         TEXT,
    source           TEXT NOT NULL DEFAULT 'yahoo_finance',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (intl_company_id, statement_type, period_type, period_end, line_item)
);

CREATE INDEX IF NOT EXISTS idx_fact_yahoo_statement_item_company_period
    ON fact_yahoo_statement_item (intl_company_id, period_end DESC, statement_type);

CREATE INDEX IF NOT EXISTS idx_fact_yahoo_statement_item_line_item
    ON fact_yahoo_statement_item (line_item);

CREATE TABLE IF NOT EXISTS fact_prices_intl (
    date             DATE NOT NULL,
    intl_company_id  TEXT NOT NULL REFERENCES dim_company_intl(intl_company_id) ON DELETE CASCADE,
    ticker           TEXT NOT NULL,
    close            DOUBLE PRECISION,
    adj_close        DOUBLE PRECISION,
    return           DOUBLE PRECISION,
    log_return       DOUBLE PRECISION,
    abs_diff         DOUBLE PRECISION,
    volume           BIGINT,
    currency         TEXT,
    region           TEXT,
    country_code     TEXT,
    exchange         TEXT,
    source           TEXT NOT NULL DEFAULT 'yahoo_finance',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (intl_company_id, date)
);

CREATE INDEX IF NOT EXISTS idx_fact_prices_intl_ticker_date
    ON fact_prices_intl (ticker, date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_prices_intl_region_date
    ON fact_prices_intl (region, date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_prices_intl_country_date
    ON fact_prices_intl (country_code, date DESC);

CREATE TABLE IF NOT EXISTS stage_yahoo_discovery (
    run_id             UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    index_code         TEXT NOT NULL,
    source_name        TEXT NOT NULL,
    source_url         TEXT,
    source_rank        INTEGER,
    raw_ticker         TEXT,
    primary_ticker     TEXT NOT NULL,
    constituent_name   TEXT,
    country_code       TEXT,
    exchange_suffix    TEXT,
    raw_payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, index_code, primary_ticker)
);

CREATE INDEX IF NOT EXISTS idx_stage_yahoo_discovery_ticker
    ON stage_yahoo_discovery (primary_ticker);

CREATE TABLE IF NOT EXISTS stage_yahoo_fundamentals (
    run_id           UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    intl_company_id  TEXT NOT NULL,
    primary_ticker   TEXT NOT NULL,
    payload_type     TEXT NOT NULL,
    item_key         TEXT NOT NULL,
    period_end       DATE NOT NULL DEFAULT CURRENT_DATE,
    value            NUMERIC,
    value_text       TEXT,
    currency         TEXT,
    raw_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, intl_company_id, payload_type, item_key, period_end)
);

CREATE INDEX IF NOT EXISTS idx_stage_yahoo_fundamentals_ticker
    ON stage_yahoo_fundamentals (primary_ticker);

CREATE TABLE IF NOT EXISTS stage_yahoo_prices (
    run_id           UUID NOT NULL REFERENCES pipeline_stage_run(run_id) ON DELETE CASCADE,
    intl_company_id  TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    date             DATE NOT NULL,
    close            DOUBLE PRECISION,
    adj_close        DOUBLE PRECISION,
    return           DOUBLE PRECISION,
    log_return       DOUBLE PRECISION,
    abs_diff         DOUBLE PRECISION,
    volume           BIGINT,
    currency         TEXT,
    region           TEXT,
    country_code     TEXT,
    exchange         TEXT,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, intl_company_id, date)
);

CREATE INDEX IF NOT EXISTS idx_stage_yahoo_prices_ticker
    ON stage_yahoo_prices (ticker, date DESC);

COMMENT ON TABLE dim_company_intl IS
    'International non-US, non-JP company master records discovered through Yahoo Finance-oriented sources.';

COMMENT ON TABLE ref_yahoo_index IS
    'Configured non-Japan Yahoo global index universe and discovery metadata.';

COMMENT ON TABLE ref_yahoo_index_constituent IS
    'Point-in-time active/stale index memberships for dim_company_intl companies discovered from Yahoo-oriented sources.';

COMMENT ON TABLE fact_yahoo_fundamental_metric IS
    'Yahoo Finance profile and valuation metrics for international equities, stored separately from XBRL standardized fundamentals.';

COMMENT ON TABLE fact_yahoo_statement_item IS
    'Yahoo Finance statement rows for international equities, keyed by dim_company_intl.';

COMMENT ON TABLE fact_prices_intl IS
    'Historical Yahoo Finance OHLCV prices for non-US, non-JP international equities keyed by dim_company_intl.';

COMMENT ON TABLE stage_yahoo_prices IS
    'Run-scoped staging rows for international Yahoo Finance price history before merging into fact_prices_intl.';
