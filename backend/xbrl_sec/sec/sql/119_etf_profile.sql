-- 119_etf_profile.sql
-- ETF characterization layer: provider/clean-name, asset-class split, top
-- holdings, sector weightings, and Fama-French factor loadings. Sourced from
-- yfinance funds_data + the existing FF factor library. Idempotent.

-- Per-ISIN profile: cleaned display name, issuer/family, asset-class split,
-- headline valuation ratios. One row per ISIN.
CREATE TABLE IF NOT EXISTS sec.dim_etf_profile (
    isin            VARCHAR(12) PRIMARY KEY REFERENCES sec.dim_etf(isin),
    clean_name      TEXT,                 -- yfinance longName (cleaner than FIRDS)
    fund_family     TEXT,                 -- e.g. "BlackRock Asset Management Ireland"
    category        TEXT,                 -- yfinance category, if present
    yf_ticker       TEXT,                 -- which yfinance symbol resolved
    stock_pct       NUMERIC(6,4),         -- asset-class split (0..1)
    bond_pct        NUMERIC(6,4),
    cash_pct        NUMERIC(6,4),
    other_pct       NUMERIC(6,4),
    pe_ratio        NUMERIC(10,4),        -- portfolio price/earnings
    pb_ratio        NUMERIC(10,4),        -- portfolio price/book
    holdings_count  INT,                  -- # underlying holdings if reported
    profile_status  VARCHAR(20) DEFAULT 'pending',  -- pending | complete | empty | failed
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Top-N holdings per ETF (yfinance gives top 10). rank 1 = largest weight.
CREATE TABLE IF NOT EXISTS sec.etf_holding (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    rank        INT NOT NULL,
    symbol      TEXT,
    holding_isin TEXT,
    name        TEXT,
    weight      NUMERIC(8,5),             -- 0..1
    cik         TEXT,
    edinet_code TEXT,
    logo_url    TEXT,
    resolved_company_id TEXT,
    resolution_source TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (isin, rank)
);

ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS holding_isin TEXT;
ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS cik TEXT;
ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS edinet_code TEXT;
ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS logo_url TEXT;
ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS resolved_company_id TEXT;
ALTER TABLE sec.etf_holding ADD COLUMN IF NOT EXISTS resolution_source TEXT;

-- Sector weightings per ETF. sector is yfinance's snake_case key normalized to
-- a Title-case label by the writer.
CREATE TABLE IF NOT EXISTS sec.etf_sector_weight (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    sector      TEXT NOT NULL,
    weight      NUMERIC(8,5),             -- 0..1
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (isin, sector)
);

-- Industry and credit-quality buckets are reported only for some yfinance
-- funds_data objects. They are separated from sector_weight to avoid mixing
-- equity GICS-like exposure with issuer/industry or bond rating buckets.
CREATE TABLE IF NOT EXISTS sec.etf_industry_weight (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    industry    TEXT NOT NULL,
    weight      NUMERIC(8,5),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (isin, industry)
);

CREATE TABLE IF NOT EXISTS sec.etf_credit_quality_weight (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    rating      TEXT NOT NULL,
    weight      NUMERIC(8,5),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (isin, rating)
);

-- Snapshot layer. Current tables above remain the read-optimized surface; these
-- tables preserve each yfinance pull with the date the snapshot represents and
-- the actual fetch timestamp.
CREATE TABLE IF NOT EXISTS sec.etf_profile_snapshot (
    isin                 VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    as_of_date           DATE NOT NULL DEFAULT DATE '2026-06-26',
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source               TEXT NOT NULL DEFAULT 'yfinance',
    source_payload_hash  TEXT,
    clean_name           TEXT,
    fund_family          TEXT,
    category             TEXT,
    yf_ticker            TEXT,
    stock_pct            NUMERIC(6,4),
    bond_pct             NUMERIC(6,4),
    cash_pct             NUMERIC(6,4),
    other_pct            NUMERIC(6,4),
    pe_ratio             NUMERIC(10,4),
    pb_ratio             NUMERIC(10,4),
    holdings_count       INT,
    profile_status       VARCHAR(20) DEFAULT 'pending',
    missing_flags        JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (isin, as_of_date, source)
);

CREATE TABLE IF NOT EXISTS sec.etf_holding_snapshot (
    isin                 VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    as_of_date           DATE NOT NULL DEFAULT DATE '2026-06-26',
    source               TEXT NOT NULL DEFAULT 'yfinance',
    rank                 INT NOT NULL,
    symbol               TEXT,
    holding_isin         TEXT,
    name                 TEXT,
    weight               NUMERIC(8,5),
    cik                  TEXT,
    edinet_code          TEXT,
    logo_url             TEXT,
    resolved_company_id  TEXT,
    resolution_source    TEXT,
    fetched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date, source, rank)
);

CREATE TABLE IF NOT EXISTS sec.etf_sector_weight_snapshot (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    as_of_date  DATE NOT NULL DEFAULT DATE '2026-06-26',
    source      TEXT NOT NULL DEFAULT 'yfinance',
    sector      TEXT NOT NULL,
    weight      NUMERIC(8,5),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date, source, sector)
);

CREATE TABLE IF NOT EXISTS sec.etf_industry_weight_snapshot (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    as_of_date  DATE NOT NULL DEFAULT DATE '2026-06-26',
    source      TEXT NOT NULL DEFAULT 'yfinance',
    industry    TEXT NOT NULL,
    weight      NUMERIC(8,5),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date, source, industry)
);

CREATE TABLE IF NOT EXISTS sec.etf_credit_quality_weight_snapshot (
    isin        VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    as_of_date  DATE NOT NULL DEFAULT DATE '2026-06-26',
    source      TEXT NOT NULL DEFAULT 'yfinance',
    rating      TEXT NOT NULL,
    weight      NUMERIC(8,5),
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (isin, as_of_date, source, rating)
);

-- Fama-French factor loadings per ETF. Mirrors sec.fact_factor_loadings (the
-- equity version) but keyed by ISIN. Single most-recent window per (isin,model).
CREATE TABLE IF NOT EXISTS sec.fact_etf_factor_loadings (
    isin          VARCHAR(12) NOT NULL REFERENCES sec.dim_etf(isin),
    model         TEXT NOT NULL,          -- FF3 | FF5 | FF6
    window_end    DATE NOT NULL,
    window_start  DATE,
    ff_region     TEXT,                   -- US | JP | INTL | EM
    n_obs         INT,
    alpha         DOUBLE PRECISION,       -- annualized intercept
    beta_mkt      DOUBLE PRECISION,
    beta_smb      DOUBLE PRECISION,
    beta_hml      DOUBLE PRECISION,
    beta_mom      DOUBLE PRECISION,
    beta_rmw      DOUBLE PRECISION,
    beta_cma      DOUBLE PRECISION,
    t_mkt         DOUBLE PRECISION,
    t_smb         DOUBLE PRECISION,
    t_hml         DOUBLE PRECISION,
    t_mom         DOUBLE PRECISION,
    t_rmw         DOUBLE PRECISION,
    t_cma         DOUBLE PRECISION,
    r2            DOUBLE PRECISION,
    adj_r2        DOUBLE PRECISION,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (isin, model, window_end)
);

CREATE INDEX IF NOT EXISTS idx_etf_holding_isin ON sec.etf_holding(isin);
CREATE INDEX IF NOT EXISTS idx_etf_sector_isin  ON sec.etf_sector_weight(isin);
CREATE INDEX IF NOT EXISTS idx_etf_industry_isin ON sec.etf_industry_weight(isin);
CREATE INDEX IF NOT EXISTS idx_etf_credit_quality_isin ON sec.etf_credit_quality_weight(isin);
CREATE INDEX IF NOT EXISTS idx_etf_profile_snapshot_isin ON sec.etf_profile_snapshot(isin, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_etf_holding_snapshot_isin ON sec.etf_holding_snapshot(isin, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_etf_ff_isin       ON sec.fact_etf_factor_loadings(isin);
