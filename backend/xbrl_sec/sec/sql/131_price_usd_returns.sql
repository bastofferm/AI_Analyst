-- Add USD-normalized price and return columns to all three price fact tables,
-- and add shares_outstanding + jurisdiction to fact_prices_intl for structural
-- parity with fact_prices_us / fact_prices_jp.
--
-- Semantics:
--   close, adj_close, return, log_return, abs_diff       => trading-currency (local)
--   close_usd, adj_close_usd,
--   return_usd, log_return_usd, abs_diff_usd             => USD-normalized
--   fx_rate_usd_per_unit                                 => usd_per_unit on that date
--                                                          (audit trail; NULL where FX missing)

SET search_path TO sec, public;

-- ----- fact_prices_us (trivially USD, columns kept for structural parity) -----
ALTER TABLE fact_prices_us
    ADD COLUMN IF NOT EXISTS close_usd            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS adj_close_usd        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_usd           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS log_return_usd       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS abs_diff_usd         DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS fx_rate_usd_per_unit DOUBLE PRECISION;

-- ----- fact_prices_jp -----
ALTER TABLE fact_prices_jp
    ADD COLUMN IF NOT EXISTS close_usd            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS adj_close_usd        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_usd           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS log_return_usd       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS abs_diff_usd         DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS fx_rate_usd_per_unit DOUBLE PRECISION;

-- ----- fact_prices_intl -----
ALTER TABLE fact_prices_intl
    ADD COLUMN IF NOT EXISTS close_usd            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS adj_close_usd        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_usd           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS log_return_usd       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS abs_diff_usd         DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS fx_rate_usd_per_unit DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS shares_outstanding   BIGINT,
    ADD COLUMN IF NOT EXISTS jurisdiction         TEXT NOT NULL DEFAULT 'GLOBAL';

-- ----- stage_yahoo_prices -----
ALTER TABLE stage_yahoo_prices
    ADD COLUMN IF NOT EXISTS close_usd            DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS adj_close_usd        DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_usd           DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS log_return_usd       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS abs_diff_usd         DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS fx_rate_usd_per_unit DOUBLE PRECISION;

-- Convenience index for backfills / cross-currency queries.
CREATE INDEX IF NOT EXISTS idx_fact_prices_intl_currency_date
    ON fact_prices_intl (currency, date);

CREATE INDEX IF NOT EXISTS idx_fact_prices_intl_jurisdiction
    ON fact_prices_intl (jurisdiction, date DESC);

COMMENT ON COLUMN fact_prices_intl.close_usd IS
    'Close price converted to USD via fact_fx.usd_per_unit on the same date.';
COMMENT ON COLUMN fact_prices_intl.return_usd IS
    'Daily return computed on adj_close_usd (USD-normalized). NULL where FX missing.';
COMMENT ON COLUMN fact_prices_intl.fx_rate_usd_per_unit IS
    'Audit trail: fact_fx.usd_per_unit lookup used to derive the *_usd columns.';
