-- 117_etf_tables.sql
-- ESMA FIRDS ETF data layer (DOC WA0006 §3). DE/AT-listed UCITS ETFs.
-- Idempotent: apply_schema re-runs every file, so all DDL guards with IF NOT EXISTS.
-- Objects live in schema `sec` (search_path is set by xbrl_sec.sec.db.connection).

-- 3.1 Master ETF dimension. One row per ISIN.
CREATE TABLE IF NOT EXISTS sec.dim_etf (
    isin               VARCHAR(12) PRIMARY KEY,
    full_name          TEXT NOT NULL,
    short_name         TEXT,
    issuer_name        TEXT,
    issuer_lei         VARCHAR(20),
    index_tracked      TEXT,
    asset_class        VARCHAR(50),       -- Equity, Fixed Income, Commodity, Mixed
    replication_method VARCHAR(50),       -- Physical, Synthetic, Sampling
    fund_currency      VARCHAR(3),
    ter_pct            NUMERIC(6,4),      -- Total Expense Ratio e.g. 0.0020
    aum_eur            NUMERIC(20,2),     -- AUM in EUR, may be NULL
    sfdr_article       VARCHAR(10),       -- Article 6, 8, or 9
    inception_date     DATE,
    is_active          BOOLEAN DEFAULT TRUE,
    termination_date   DATE,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

-- 3.2 One row per ETF x trading venue. An ETF may list on multiple exchanges.
CREATE TABLE IF NOT EXISTS sec.dim_etf_listing (
    id                 SERIAL PRIMARY KEY,
    isin               VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    mic                VARCHAR(6) NOT NULL,        -- e.g. XETR, XWBO
    exchange_ticker    VARCHAR(20),                -- e.g. EXS1, TRET
    trading_currency   VARCHAR(3),
    country            VARCHAR(2),                 -- DE, AT
    is_primary_listing BOOLEAN DEFAULT FALSE,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, mic)
);

-- 3.3 Daily OHLCV per ETF x listing. Mirrors fact_prices_us structure.
CREATE TABLE IF NOT EXISTS sec.fact_prices_etf (
    id          BIGSERIAL PRIMARY KEY,
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    mic         VARCHAR(6) NOT NULL,
    price_date  DATE NOT NULL,
    open        NUMERIC(18,6),
    high        NUMERIC(18,6),
    low         NUMERIC(18,6),
    close       NUMERIC(18,6) NOT NULL,
    volume      BIGINT,
    currency    VARCHAR(3),
    source      VARCHAR(20) DEFAULT 'yfinance',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(isin, mic, price_date)
);

CREATE INDEX IF NOT EXISTS idx_fact_prices_etf_isin_date
    ON sec.fact_prices_etf(isin, price_date DESC);

-- 3.4 State management for the ETF price pipeline. Mirrors pipeline_entity_state.
CREATE TABLE IF NOT EXISTS sec.pipeline_etf_state (
    isin            VARCHAR(12) PRIMARY KEY REFERENCES sec.dim_etf(isin),
    price_stage     VARCHAR(30) DEFAULT 'pending',  -- pending, downloading, complete, failed
    last_price_date DATE,
    last_run_at     TIMESTAMPTZ,
    error_message   TEXT,
    retry_count     INT DEFAULT 0
);

-- 3.5 Tracks each FIRDS file download and parse run.
CREATE TABLE IF NOT EXISTS sec.pipeline_firds_run (
    id                 SERIAL PRIMARY KEY,
    file_type          VARCHAR(10) NOT NULL,   -- FULINS or DLTINS
    file_date          DATE NOT NULL,
    file_url           TEXT NOT NULL,
    file_md5           VARCHAR(32),
    status             VARCHAR(20) DEFAULT 'pending',
    instruments_parsed INT,
    etfs_upserted      INT,
    run_started_at     TIMESTAMPTZ,
    run_completed_at   TIMESTAMPTZ,
    error_message      TEXT
);
