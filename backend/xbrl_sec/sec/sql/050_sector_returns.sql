-- Sector / Industry-Group cap-weighted returns + per-constituent weight snapshots.
--
-- Two new fact tables:
--   * fact_sector_returns  — daily cap-weighted return + index level
--                            keyed by (jurisdiction, grouping_level, gics_code, date)
--   * fact_sector_weights  — monthly constituent weight snapshots
--                            keyed by (jurisdiction, grouping_level, gics_code, snapshot_date, ticker)
--
-- Plus 3 new columns on dim_company_jp to store the yfinance shares snapshot
-- (US already derives shares from fact_fundamentals_std_us.shares_outstanding_diluted).

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- fact_sector_returns
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_sector_returns (
    jurisdiction        TEXT             NOT NULL CHECK (jurisdiction IN ('US','JP')),
    grouping_level      TEXT             NOT NULL CHECK (grouping_level IN ('sector','industry_group')),
    gics_code           TEXT             NOT NULL,
    gics_name           TEXT             NOT NULL,
    date                DATE             NOT NULL,
    cap_weighted_return DOUBLE PRECISION,
    level               DOUBLE PRECISION,
    total_market_cap    DOUBLE PRECISION,
    constituent_count   INT,
    updated_at          TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, grouping_level, gics_code, date)
);

CREATE INDEX IF NOT EXISTS idx_sector_returns_date
    ON fact_sector_returns (date DESC, jurisdiction, grouping_level);

CREATE INDEX IF NOT EXISTS idx_sector_returns_group
    ON fact_sector_returns (jurisdiction, grouping_level, gics_code, date DESC);

COMMENT ON TABLE fact_sector_returns IS
    'Daily cap-weighted return and index level for GICS Sector + Industry Group buckets, both jurisdictions.';

-- ---------------------------------------------------------------------------
-- fact_sector_weights — monthly constituent snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_sector_weights (
    jurisdiction    TEXT             NOT NULL CHECK (jurisdiction IN ('US','JP')),
    grouping_level  TEXT             NOT NULL CHECK (grouping_level IN ('sector','industry_group')),
    gics_code       TEXT             NOT NULL,
    snapshot_date   DATE             NOT NULL,
    ticker          TEXT             NOT NULL,
    market_cap      DOUBLE PRECISION NOT NULL,
    weight          DOUBLE PRECISION NOT NULL,
    updated_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, grouping_level, gics_code, snapshot_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_sector_weights_ticker
    ON fact_sector_weights (ticker, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_sector_weights_snapshot
    ON fact_sector_weights (snapshot_date DESC, jurisdiction, grouping_level);

COMMENT ON TABLE fact_sector_weights IS
    'Month-end constituent weight snapshots — lets us follow portfolio drift through time.';

-- ---------------------------------------------------------------------------
-- dim_company_us / dim_company_jp — store yfinance sharesOutstanding snapshot
-- ---------------------------------------------------------------------------
-- US shares are also available from fact_fundamentals_std_us (XBRL filings),
-- but coverage is partial (~9% of tickers). yfinance gives ≈full coverage as a
-- fallback for the sector-returns compute job.

ALTER TABLE dim_company_us
    ADD COLUMN IF NOT EXISTS shares_outstanding   BIGINT,
    ADD COLUMN IF NOT EXISTS shares_source        TEXT,
    ADD COLUMN IF NOT EXISTS shares_updated_at    TIMESTAMPTZ;

ALTER TABLE dim_company_jp
    ADD COLUMN IF NOT EXISTS shares_outstanding   BIGINT,
    ADD COLUMN IF NOT EXISTS shares_source        TEXT,
    ADD COLUMN IF NOT EXISTS shares_updated_at    TIMESTAMPTZ;

COMMENT ON COLUMN dim_company_us.shares_outstanding IS
    'Latest sharesOutstanding from yfinance Ticker.info — fallback for sector_returns_compute when fact_fundamentals_std_us is missing.';
COMMENT ON COLUMN dim_company_jp.shares_outstanding IS
    'Latest sharesOutstanding from yfinance Ticker.info — used by sector_returns_compute to derive daily market cap.';
