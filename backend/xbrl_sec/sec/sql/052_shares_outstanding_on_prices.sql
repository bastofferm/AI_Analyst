-- Move historical shares-outstanding alongside prices.
--
-- Until now, the sector_returns_compute job reconstructed daily shares via a
-- LATERAL subquery into fact_fundamentals_std_us (US) or a static snapshot in
-- dim_company_jp.shares_outstanding (JP). The latter ignores issuance and
-- buybacks. This migration adds a per-row shares_outstanding column to the
-- price fact tables; a separate backfill script forward-fills it from
-- fact_fundamentals_std_{us,jp} using each filing's period_end as the start
-- of a half-open validity interval.
--
-- dim_company_{us,jp}.shares_outstanding is kept as a yfinance "current"
-- snapshot for non-historical use, but is no longer read by the sector compute.

SET search_path TO sec, public;

ALTER TABLE fact_prices_us
    ADD COLUMN IF NOT EXISTS shares_outstanding BIGINT;

ALTER TABLE fact_prices_jp
    ADD COLUMN IF NOT EXISTS shares_outstanding BIGINT;

-- Partial index keeps the index small while the backfill is incremental.
-- (ticker, date DESC) supports the "as-of" probe used by recency joins.
CREATE INDEX IF NOT EXISTS idx_fact_prices_us_ticker_date_shares
    ON fact_prices_us (ticker, date DESC)
    WHERE shares_outstanding IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fact_prices_jp_ticker_date_shares
    ON fact_prices_jp (ticker, date DESC)
    WHERE shares_outstanding IS NOT NULL;

COMMENT ON COLUMN fact_prices_us.shares_outstanding IS
    'Forward-filled from fact_fundamentals_std_us.shares_outstanding_diluted per (cik, period_end). Carries most-recent filing on-or-before date.';

COMMENT ON COLUMN fact_prices_jp.shares_outstanding IS
    'Forward-filled from fact_fundamentals_std_jp (line_item_id=''shares_outstanding'') per (edinet_code, period_end). Carries most-recent filing on-or-before date.';
