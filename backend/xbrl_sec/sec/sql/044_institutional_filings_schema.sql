-- SEC institutional ownership filings: 13F and 13D/G.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_13f_manager (
    manager_cik TEXT PRIMARY KEY,
    manager_name TEXT NOT NULL,
    manager_type TEXT NOT NULL DEFAULT 'unknown',
    is_public_company BOOLEAN NOT NULL DEFAULT false,
    public_entity_cik TEXT,
    name_source TEXT,
    crd_number TEXT,
    sec_file_number TEXT,
    form_13f_file_number TEXT,
    report_type TEXT,
    street1 TEXT,
    street2 TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    filing_count_primary INTEGER NOT NULL DEFAULT 0,
    filing_count_other INTEGER NOT NULL DEFAULT 0,
    filing_count_total INTEGER NOT NULL DEFAULT 0,
    first_quarter_filed DATE,
    last_quarter_filed DATE,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dim_13f_manager_name
    ON dim_13f_manager (manager_name);

CREATE INDEX IF NOT EXISTS idx_dim_13f_manager_name_source
    ON dim_13f_manager (name_source);

CREATE TABLE IF NOT EXISTS source_13f_dataset_state (
    dataset_key TEXT PRIMARY KEY,
    dataset_url TEXT,
    period_label TEXT,
    local_path TEXT,
    downloaded BOOLEAN NOT NULL DEFAULT false,
    downloaded_at TIMESTAMPTZ,
    download_error TEXT,
    parsed BOOLEAN NOT NULL DEFAULT false,
    parsed_at TIMESTAMPTZ,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    source_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_13f_filing_state (
    manager_cik TEXT NOT NULL REFERENCES dim_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    accession_number TEXT NOT NULL,
    filing_type TEXT,
    filed_date DATE,
    dataset_key TEXT,
    parsed BOOLEAN NOT NULL DEFAULT false,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    is_latest_amendment BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period, accession_number)
);

CREATE INDEX IF NOT EXISTS idx_source_13f_filing_state_report_period
    ON source_13f_filing_state (report_period DESC, parsed);

CREATE TABLE IF NOT EXISTS fact_13f_submission (
    accession_number TEXT PRIMARY KEY,
    manager_cik TEXT NOT NULL REFERENCES dim_13f_manager(manager_cik),
    manager_name TEXT,
    filing_type TEXT,
    filed_date DATE,
    report_period DATE,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    is_latest_amendment BOOLEAN NOT NULL DEFAULT true,
    dataset_key TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_submission_manager_period
    ON fact_13f_submission (manager_cik, report_period DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_submission_dataset
    ON fact_13f_submission (dataset_key, accession_number);

CREATE TABLE IF NOT EXISTS fact_13f_coverpage (
    accession_number TEXT PRIMARY KEY REFERENCES fact_13f_submission(accession_number) ON DELETE CASCADE,
    crd_number TEXT,
    sec_file_number TEXT,
    report_type TEXT,
    form_13f_file_number TEXT,
    provide_info_for_instruction5 BOOLEAN,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_13f_summarypage (
    accession_number TEXT PRIMARY KEY REFERENCES fact_13f_submission(accession_number) ON DELETE CASCADE,
    other_included_managers_count INTEGER,
    table_entry_total INTEGER,
    table_value_total BIGINT,
    is_confidential_omitted BOOLEAN,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_13f_holdings (
    accession_number TEXT NOT NULL REFERENCES fact_13f_submission(accession_number) ON DELETE CASCADE,
    infotable_sk TEXT NOT NULL,
    manager_cik TEXT NOT NULL REFERENCES dim_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    filing_type TEXT,
    filed_date DATE,
    issuer_name TEXT,
    title_of_class TEXT,
    cusip TEXT,
    figi TEXT,
    cusip6 TEXT,
    issuer_cik TEXT,
    issuer_ticker TEXT,
    value_x1000 BIGINT,
    shares_or_principal NUMERIC(24,4),
    sh_prn_flag TEXT,
    put_call TEXT,
    investment_discretion TEXT,
    other_manager TEXT,
    voting_authority_sole BIGINT,
    voting_authority_shared BIGINT,
    voting_authority_none BIGINT,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    is_latest_amendment BOOLEAN NOT NULL DEFAULT true,
    dataset_key TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, infotable_sk)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_holdings_issuer_period
    ON fact_13f_holdings (issuer_cik, issuer_ticker, report_period DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_holdings_manager_period
    ON fact_13f_holdings (manager_cik, report_period DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_holdings_cusip
    ON fact_13f_holdings (cusip, report_period DESC);

CREATE TABLE IF NOT EXISTS source_13dg_filing_state (
    accession_number TEXT PRIMARY KEY,
    reporting_person_cik TEXT,
    issuer_cik TEXT,
    form_type TEXT,
    filed_date DATE,
    local_path TEXT,
    source_url TEXT,
    downloaded BOOLEAN NOT NULL DEFAULT false,
    parsed BOOLEAN NOT NULL DEFAULT false,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    source_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_13dg_filing_state_issuer
    ON source_13dg_filing_state (issuer_cik, filed_date DESC);

CREATE TABLE IF NOT EXISTS fact_13dg_ownership (
    accession_number TEXT NOT NULL REFERENCES source_13dg_filing_state(accession_number) ON DELETE CASCADE,
    row_ordinal INTEGER NOT NULL,
    form_type TEXT,
    filed_date DATE,
    period_of_report DATE,
    issuer_cik TEXT,
    issuer_name TEXT,
    issuer_ticker TEXT,
    title_of_class TEXT,
    cusip TEXT,
    reporting_person_cik TEXT,
    reporting_person_name TEXT,
    reporting_person_type TEXT,
    is_group_member BOOLEAN NOT NULL DEFAULT false,
    group_name TEXT,
    amount_beneficially_owned BIGINT,
    percent_of_class NUMERIC(9,4),
    sole_voting_power BIGINT,
    shared_voting_power BIGINT,
    sole_dispositive_power BIGINT,
    shared_dispositive_power BIGINT,
    aggregate_amount BIGINT,
    purpose_of_transaction TEXT,
    source_of_funds TEXT,
    raw_text_excerpt TEXT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, row_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_fact_13dg_ownership_issuer
    ON fact_13dg_ownership (issuer_cik, issuer_ticker, filed_date DESC);

CREATE OR REPLACE VIEW vw_13f_issuer_quarterly AS
WITH latest AS (
    SELECT *
    FROM fact_13f_holdings
    WHERE is_latest_amendment
      AND COALESCE(put_call, '') = ''
      AND COALESCE(sh_prn_flag, 'SH') = 'SH'
)
SELECT report_period,
       issuer_cik,
       issuer_ticker,
       COUNT(DISTINCT manager_cik) AS total_institutional_holders,
       SUM(value_x1000) AS total_institutional_value_x1000,
       SUM(shares_or_principal) AS total_institutional_shares,
       COUNT(*) FILTER (WHERE shares_or_principal > 0) AS position_rows
FROM latest
GROUP BY report_period, issuer_cik, issuer_ticker;
