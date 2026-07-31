-- SEC Form 4 insider trading filings.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS source_insider_filing_state (
    accession_number TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    form_type TEXT NOT NULL DEFAULT '4',
    filing_date DATE,
    xml_downloaded BOOLEAN NOT NULL DEFAULT false,
    xml_downloaded_at TIMESTAMPTZ,
    download_error TEXT,
    xml_parsed BOOLEAN NOT NULL DEFAULT false,
    xml_parsed_at TIMESTAMPTZ,
    parse_error TEXT,
    disk_path TEXT,
    source_url TEXT,
    source_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_insider_filing_state_cik_date
    ON source_insider_filing_state (cik, filing_date DESC);

CREATE INDEX IF NOT EXISTS idx_source_insider_filing_state_parse
    ON source_insider_filing_state (xml_downloaded, xml_parsed);

CREATE TABLE IF NOT EXISTS fact_insider_filing (
    accession_number TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    reporting_owner_cik TEXT,
    reporting_owner_name TEXT,
    is_director BOOLEAN,
    is_officer BOOLEAN,
    is_ten_percent_owner BOOLEAN,
    officer_title TEXT,
    other_text TEXT,
    issuer_name TEXT,
    issuer_trading_symbol TEXT,
    period_of_report DATE,
    filing_date DATE,
    signature_date DATE,
    document_type TEXT,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    source_path TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fact_insider_filing_cik_date
    ON fact_insider_filing (cik, filing_date DESC);

CREATE INDEX IF NOT EXISTS idx_fact_insider_filing_owner_date
    ON fact_insider_filing (reporting_owner_cik, filing_date DESC);

CREATE TABLE IF NOT EXISTS fact_insider_transaction_non_derivative (
    accession_number TEXT NOT NULL REFERENCES fact_insider_filing(accession_number) ON DELETE CASCADE,
    transaction_ordinal INTEGER NOT NULL,
    security_title TEXT,
    transaction_date DATE,
    transaction_code TEXT,
    shares_amount NUMERIC(24,4),
    price_per_share NUMERIC(24,4),
    acquired_disposed_code TEXT,
    shares_owned_following NUMERIC(24,4),
    direct_or_indirect TEXT,
    equity_swap_involved BOOLEAN NOT NULL DEFAULT false,
    footnote_ids TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, transaction_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_fact_insider_non_derivative_date
    ON fact_insider_transaction_non_derivative (transaction_date DESC, transaction_code);

CREATE TABLE IF NOT EXISTS fact_insider_transaction_derivative (
    accession_number TEXT NOT NULL REFERENCES fact_insider_filing(accession_number) ON DELETE CASCADE,
    transaction_ordinal INTEGER NOT NULL,
    security_title TEXT,
    conversion_exercise_price NUMERIC(24,4),
    transaction_date DATE,
    transaction_code TEXT,
    shares_amount NUMERIC(24,4),
    acquired_disposed_code TEXT,
    exercise_date DATE,
    expiration_date DATE,
    underlying_security_title TEXT,
    underlying_shares_amount NUMERIC(24,4),
    direct_or_indirect TEXT,
    equity_swap_involved BOOLEAN NOT NULL DEFAULT false,
    footnote_ids TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, transaction_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_fact_insider_derivative_date
    ON fact_insider_transaction_derivative (transaction_date DESC, transaction_code);

CREATE OR REPLACE VIEW vw_insider_trade_events AS
SELECT f.cik,
       d.primary_ticker AS ticker,
       f.accession_number,
       f.reporting_owner_cik,
       f.reporting_owner_name,
       f.officer_title,
       f.is_director,
       f.is_officer,
       f.is_ten_percent_owner,
       'NON_DERIVATIVE' AS transaction_table,
       t.transaction_ordinal,
       t.security_title,
       t.transaction_date,
       t.transaction_code,
       t.shares_amount,
       t.price_per_share,
       t.shares_amount * t.price_per_share AS transaction_value,
       t.acquired_disposed_code,
       t.shares_owned_following,
       t.direct_or_indirect
FROM fact_insider_filing f
JOIN fact_insider_transaction_non_derivative t USING (accession_number)
LEFT JOIN dim_company_us d ON d.cik = f.cik
UNION ALL
SELECT f.cik,
       d.primary_ticker AS ticker,
       f.accession_number,
       f.reporting_owner_cik,
       f.reporting_owner_name,
       f.officer_title,
       f.is_director,
       f.is_officer,
       f.is_ten_percent_owner,
       'DERIVATIVE' AS transaction_table,
       t.transaction_ordinal,
       t.security_title,
       t.transaction_date,
       t.transaction_code,
       t.shares_amount,
       t.conversion_exercise_price AS price_per_share,
       t.shares_amount * t.conversion_exercise_price AS transaction_value,
       t.acquired_disposed_code,
       NULL::NUMERIC AS shares_owned_following,
       t.direct_or_indirect
FROM fact_insider_filing f
JOIN fact_insider_transaction_derivative t USING (accession_number)
LEFT JOIN dim_company_us d ON d.cik = f.cik;

CREATE OR REPLACE VIEW vw_insider_ticker_activity AS
SELECT ticker,
       cik,
       transaction_date,
       transaction_code,
       acquired_disposed_code,
       COUNT(*) AS transaction_count,
       SUM(shares_amount) AS shares_amount,
       SUM(transaction_value) AS transaction_value
FROM vw_insider_trade_events
GROUP BY ticker, cik, transaction_date, transaction_code, acquired_disposed_code;
