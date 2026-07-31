-- Monthly buyback granularity + 8-K corporate-action support.
--
-- (1) fact_us_monthly_buybacks
--     Per-month share repurchases extracted from the "Issuer Purchases of
--     Equity Securities" table in Item 5 (10-K) or Item 2(c) (10-Q). One row
--     per (cik, calendar month) per filing. Lets fact_prices_us.shares_outstanding
--     step monthly instead of quarterly.
--
-- (2) dim_eightk_filing
--     Filing-level index of every SEC Form 8-K downloaded for US issuers.
--     Tracks the `items` array so the 8-K split parser can pre-filter to
--     items 5.03 (Amendments to Articles) and 8.01 (Other Events).
--
-- fact_stock_split_event (migration 036) already permits source_type='SEC_8K';
-- nothing to change there.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- fact_us_monthly_buybacks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_us_monthly_buybacks (
    cik                            TEXT  NOT NULL,
    period_start                   DATE  NOT NULL,
    period_end                     DATE  NOT NULL,
    shares_purchased               BIGINT,
    avg_price_paid_per_share       NUMERIC,
    shares_under_program_remaining BIGINT,
    program_max_remaining_amount   NUMERIC,
    filing_form                    TEXT  NOT NULL,    -- '10-K' or '10-Q'
    filing_id                      TEXT  NOT NULL,    -- accession number
    filed_date                     DATE  NOT NULL,
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, period_start, period_end, filing_id)
);

CREATE INDEX IF NOT EXISTS idx_us_monthly_buybacks_cik_date
    ON fact_us_monthly_buybacks (cik, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_us_monthly_buybacks_period
    ON fact_us_monthly_buybacks (period_end DESC, cik)
    WHERE shares_purchased IS NOT NULL AND shares_purchased > 0;

COMMENT ON TABLE fact_us_monthly_buybacks IS
    'Per-month share repurchases parsed from Item 5 (10-K) / Item 2(c) (10-Q) "Issuer Purchases of Equity Securities" HTML tables.';

-- ---------------------------------------------------------------------------
-- dim_eightk_filing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_eightk_filing (
    cik              TEXT NOT NULL,
    accession        TEXT NOT NULL,
    filed_date       DATE NOT NULL,
    period_of_report DATE,
    items            TEXT[],                            -- e.g. {'1.01','5.03','8.01'}
    primary_doc      TEXT,                              -- filename of the primary document
    raw_url          TEXT NOT NULL,                     -- canonical EDGAR archive URL
    local_path       TEXT,                              -- relative to market_data/
    file_size_bytes  BIGINT,
    downloaded_at    TIMESTAMPTZ,
    parsed_at        TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, accession)
);

CREATE INDEX IF NOT EXISTS idx_dim_eightk_filing_items
    ON dim_eightk_filing USING gin (items);

CREATE INDEX IF NOT EXISTS idx_dim_eightk_filing_date
    ON dim_eightk_filing (filed_date DESC, cik);

CREATE INDEX IF NOT EXISTS idx_dim_eightk_filing_unparsed
    ON dim_eightk_filing (cik, accession)
    WHERE downloaded_at IS NOT NULL AND parsed_at IS NULL;

COMMENT ON TABLE dim_eightk_filing IS
    'Index of every SEC Form 8-K downloaded for US issuers. items[] enables item-code-driven filtering by downstream parsers.';
COMMENT ON COLUMN dim_eightk_filing.items IS
    'Item codes disclosed in this 8-K (e.g. ARRAY[''1.01'',''5.03'',''8.01'']). Source: SEC submissions feed.';
