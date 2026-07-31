-- 120_etf_fx.sql
-- FX support for currency-correct ETF factor regressions. Fama-French factors
-- are USD-denominated, but our ETF price series are quoted in the local listing
-- currency (EUR/GBP/USD/CHF...). To regress fund returns on USD factors we must
-- convert fund returns to USD: for log returns this is exactly additive,
--     r_usd = r_local + r_fx,   r_fx = Δlog(USD per 1 unit of local ccy).
-- Idempotent.

-- Daily FX rates expressed as USD per 1 unit of the quote currency.
-- USD itself is stored as 1.0 so the join is uniform.
CREATE TABLE IF NOT EXISTS sec.fact_fx (
    ccy          VARCHAR(3) NOT NULL,
    fx_date      DATE NOT NULL,
    usd_per_unit DOUBLE PRECISION NOT NULL,
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ccy, fx_date)
);
CREATE INDEX IF NOT EXISTS idx_fact_fx_date ON sec.fact_fx(fx_date);

-- Quote currency of each ETF's price series (from yfinance fast_info). Distinct
-- from dim_etf.fund_currency, which is the fund's NAV/base currency.
ALTER TABLE sec.dim_etf_profile ADD COLUMN IF NOT EXISTS quote_ccy VARCHAR(3);
