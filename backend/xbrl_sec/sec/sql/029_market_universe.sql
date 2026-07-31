-- Market universe tables: rename fact_macro_fred → fact_macro,
-- create fact_cross_asset (ETF proxy prices) and fact_fama_french (daily factors).
--
-- fact_macro_fred was created in 028 and is now renamed to match the name
-- the dashboard (ops_dashboard.py) expects throughout its queries.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Rename fact_macro_fred → fact_macro
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('sec.fact_macro') IS NULL
       AND to_regclass('sec.fact_macro_fred') IS NOT NULL THEN
        ALTER TABLE fact_macro_fred RENAME TO fact_macro;
    END IF;
END $$;

-- Rename the date index so it reflects the new table name
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'sec' AND tablename = 'fact_macro'
          AND indexname = 'idx_macro_fred_date'
    ) THEN
        ALTER INDEX idx_macro_fred_date RENAME TO idx_macro_date;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Cross-asset ETF proxy prices
-- (equity indices, fixed income, commodities, FX, volatility via ETF proxies)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_cross_asset (
    date        DATE NOT NULL,
    ticker      TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    close       DOUBLE PRECISION,
    adj_close   DOUBLE PRECISION,
    return      DOUBLE PRECISION,
    log_return  DOUBLE PRECISION,
    volume      BIGINT,
    currency    TEXT NOT NULL DEFAULT 'USD',
    PRIMARY KEY (ticker, date)
);

COMMENT ON TABLE fact_cross_asset IS
    'Daily prices for cross-asset ETF proxies: equity indices, fixed income, commodities, FX, volatility.';
COMMENT ON COLUMN fact_cross_asset.asset_class IS
    'Asset class bucket: equity_index | fixed_income | commodity | fx | volatility';

CREATE INDEX IF NOT EXISTS idx_cross_asset_date ON fact_cross_asset (date DESC, asset_class);
CREATE INDEX IF NOT EXISTS idx_cross_asset_ticker ON fact_cross_asset (ticker, date DESC);

-- ---------------------------------------------------------------------------
-- Fama-French daily factors (FF3, FF5, FF6 + Momentum)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_fama_french (
    date        DATE NOT NULL,
    factor      TEXT NOT NULL,
    value       DOUBLE PRECISION,
    dataset     TEXT NOT NULL DEFAULT 'F-F_Research_Data_Factors_daily',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (factor, date)
);

COMMENT ON TABLE fact_fama_french IS
    'Daily Fama-French factor returns from Ken French data library: Mkt-RF, SMB, HML, RMW, CMA, Mom, RF.';
COMMENT ON COLUMN fact_fama_french.factor IS
    'Factor name: Mkt-RF | SMB | HML | RMW | CMA | Mom | RF';

CREATE INDEX IF NOT EXISTS idx_fama_french_date ON fact_fama_french (date DESC, factor);
