SET search_path TO sec, public;

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_cusip_latest_period
    ON core_13f_holding ((upper(cusip)), report_period DESC)
    WHERE cusip IS NOT NULL
      AND is_latest_amendment = TRUE;

CREATE INDEX IF NOT EXISTS idx_dim_13f_security_us_primary_ticker_upper
    ON dim_13f_security_us ((upper(primary_ticker)))
    WHERE primary_ticker IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dim_company_us_primary_ticker_upper
    ON dim_company_us ((upper(primary_ticker)))
    WHERE primary_ticker IS NOT NULL;
