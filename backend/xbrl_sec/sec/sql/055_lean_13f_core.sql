SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS stg_13f_dataset (
    dataset_key TEXT PRIMARY KEY,
    report_period DATE,
    period_label TEXT,
    source_url TEXT,
    local_path TEXT,
    source_hash TEXT,
    downloaded BOOLEAN NOT NULL DEFAULT false,
    parsed BOOLEAN NOT NULL DEFAULT false,
    standardized BOOLEAN NOT NULL DEFAULT false,
    classified BOOLEAN NOT NULL DEFAULT false,
    downloaded_at TIMESTAMPTZ,
    parsed_at TIMESTAMPTZ,
    standardized_at TIMESTAMPTZ,
    classified_at TIMESTAMPTZ,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    filings_parsed INTEGER NOT NULL DEFAULT 0,
    holdings_parsed BIGINT NOT NULL DEFAULT 0,
    download_error TEXT,
    parse_error TEXT,
    standardize_error TEXT,
    classification_error TEXT,
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stg_13f_submission (
    accession_number TEXT PRIMARY KEY,
    dataset_key TEXT,
    manager_cik TEXT,
    manager_name TEXT,
    filing_type TEXT,
    filed_date DATE,
    report_period DATE,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    other_included_managers_count INTEGER,
    table_entry_total INTEGER,
    table_value_total NUMERIC,
    cover_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    submission_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stg_13f_submission_manager_period
    ON stg_13f_submission (manager_cik, report_period DESC);

CREATE TABLE IF NOT EXISTS stg_13f_holding (
    accession_number TEXT NOT NULL,
    row_id TEXT NOT NULL,
    row_ordinal INTEGER,
    manager_cik TEXT,
    report_period DATE,
    filing_type TEXT,
    filed_date DATE,
    issuer_name TEXT,
    title_of_class TEXT,
    cusip TEXT,
    figi TEXT,
    cusip6 TEXT,
    issuer_cik TEXT,
    issuer_ticker TEXT,
    value_reported NUMERIC,
    shares_or_principal NUMERIC(24,4),
    sh_prn_flag TEXT,
    put_call TEXT,
    investment_discretion TEXT,
    other_manager TEXT,
    voting_authority_sole BIGINT,
    voting_authority_shared BIGINT,
    voting_authority_none BIGINT,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, row_id)
);

CREATE INDEX IF NOT EXISTS idx_stg_13f_holding_manager_period
    ON stg_13f_holding (manager_cik, report_period DESC);

CREATE TABLE IF NOT EXISTS core_13f_manager (
    manager_cik TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL,
    metadata_source TEXT,
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
    first_report_period DATE,
    last_report_period DATE,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_core_13f_manager_name
    ON core_13f_manager (legal_name);

CREATE TABLE IF NOT EXISTS core_13f_filing (
    accession_number TEXT PRIMARY KEY,
    manager_cik TEXT NOT NULL REFERENCES core_13f_manager(manager_cik),
    manager_name TEXT,
    dataset_key TEXT,
    filing_type TEXT,
    filed_date DATE,
    report_period DATE NOT NULL,
    amendment_number INTEGER NOT NULL DEFAULT 0,
    is_amendment BOOLEAN NOT NULL DEFAULT false,
    is_latest_amendment BOOLEAN NOT NULL DEFAULT true,
    other_included_managers_count INTEGER,
    table_entry_total INTEGER,
    table_value_total NUMERIC,
    source_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_core_13f_filing_manager_period
    ON core_13f_filing (manager_cik, report_period DESC, is_latest_amendment);

CREATE INDEX IF NOT EXISTS idx_core_13f_filing_dataset
    ON core_13f_filing (dataset_key, accession_number);

CREATE TABLE IF NOT EXISTS core_13f_holding (
    accession_number TEXT NOT NULL REFERENCES core_13f_filing(accession_number) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    manager_cik TEXT NOT NULL,
    report_period DATE NOT NULL,
    filed_date DATE,
    is_latest_amendment BOOLEAN NOT NULL DEFAULT true,
    issuer_name TEXT,
    title_of_class TEXT,
    cusip TEXT,
    figi TEXT,
    cusip6 TEXT,
    issuer_cik TEXT,
    issuer_ticker TEXT,
    asset_bucket TEXT NOT NULL DEFAULT 'other',
    value_reported NUMERIC,
    price_at_filing NUMERIC,
    market_value_usd NUMERIC,
    shares_or_principal NUMERIC(24,4),
    sh_prn_flag TEXT,
    put_call TEXT,
    investment_discretion TEXT,
    other_manager TEXT,
    voting_authority_sole BIGINT,
    voting_authority_shared BIGINT,
    voting_authority_none BIGINT,
    issuer_resolution_status TEXT,
    price_covered BOOLEAN NOT NULL DEFAULT false,
    factor_covered BOOLEAN NOT NULL DEFAULT false,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accession_number, row_id)
);

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_manager_period
    ON core_13f_holding (manager_cik, report_period DESC, is_latest_amendment);

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_issuer
    ON core_13f_holding (issuer_cik, issuer_ticker, report_period DESC);

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_report_period
    ON core_13f_holding (report_period DESC, is_latest_amendment);

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_cusip_upper
    ON core_13f_holding ((upper(cusip)))
    WHERE cusip IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_core_13f_holding_cusip6
    ON core_13f_holding (cusip6)
    WHERE cusip6 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dim_company_us_isin_cusip
    ON dim_company_us ((upper(substring(isin from 3 for 9))))
    WHERE isin IS NOT NULL;

CREATE TABLE IF NOT EXISTS dim_13f_security_us (
    cusip TEXT PRIMARY KEY,
    cusip8 TEXT,
    cusip6 TEXT,
    isin TEXT,
    issuer_cik TEXT,
    primary_ticker TEXT,
    issuer_name TEXT,
    security_title TEXT,
    asset_bucket TEXT NOT NULL DEFAULT 'other',
    sector TEXT,
    industry_group TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
    confidence_score NUMERIC,
    source_name TEXT,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    row_count BIGINT NOT NULL DEFAULT 0,
    value_observed NUMERIC,
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dim_13f_security_us_status
    ON dim_13f_security_us (resolution_status, asset_bucket);

CREATE INDEX IF NOT EXISTS idx_dim_13f_security_us_ticker
    ON dim_13f_security_us (primary_ticker);

CREATE TABLE IF NOT EXISTS fact_13f_cusip_llm_comparison (
    cusip TEXT NOT NULL,
    cusip8 TEXT,
    cusip6 TEXT,
    observed_issuer_name TEXT,
    observed_security_title TEXT,
    deterministic_status TEXT,
    candidate_cik TEXT,
    candidate_ticker TEXT,
    candidate_name TEXT,
    confidence NUMERIC,
    accepted BOOLEAN NOT NULL DEFAULT false,
    rationale TEXT,
    candidate_count INTEGER,
    value_observed NUMERIC,
    row_count BIGINT,
    model TEXT NOT NULL,
    raw_response TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cusip, model)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_cusip_llm_comparison_accept
    ON fact_13f_cusip_llm_comparison (accepted, confidence DESC);

CREATE TABLE IF NOT EXISTS core_13f_manager_period (
    manager_cik TEXT NOT NULL REFERENCES core_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    latest_accession_number TEXT,
    filed_date DATE,
    filing_type TEXT,
    portfolio_value_reported NUMERIC,
    portfolio_value_market NUMERIC,
    long_market_value NUMERIC,
    equity_value NUMERIC,
    fixed_income_value NUMERIC,
    fund_etf_value NUMERIC,
    derivatives_value NUMERIC,
    other_value NUMERIC,
    equity_pct NUMERIC,
    fixed_income_pct NUMERIC,
    fund_etf_pct NUMERIC,
    derivatives_pct NUMERIC,
    other_pct NUMERIC,
    position_count INTEGER,
    derivative_position_count INTEGER,
    top_5_concentration NUMERIC,
    top_10_concentration NUMERIC,
    max_position_weight NUMERIC,
    options_ratio NUMERIC,
    unresolved_value NUMERIC,
    unresolved_weight NUMERIC,
    price_coverage_weight NUMERIC,
    factor_coverage_weight NUMERIC,
    beta_mkt NUMERIC,
    beta_smb NUMERIC,
    beta_hml NUMERIC,
    beta_mom NUMERIC,
    beta_rmw NUMERIC,
    beta_cma NUMERIC,
    factor_var_95_1d NUMERIC,
    factor_cvar_95_1d NUMERIC,
    factor_observations INTEGER,
    median_turnover_rate NUMERIC,
    max_turnover_rate NUMERIC,
    median_options_ratio_8q NUMERIC,
    mean_position_count_8q NUMERIC,
    shares_voting_sole_pct NUMERIC,
    metrics_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period)
);

CREATE INDEX IF NOT EXISTS idx_core_13f_manager_period_latest_value
    ON core_13f_manager_period (report_period DESC, portfolio_value_market DESC);

CREATE TABLE IF NOT EXISTS ref_13f_manager_style (
    reference_id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_rank INTEGER,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    domicile_or_headquarters TEXT,
    strategy_or_profile TEXT,
    target_label TEXT NOT NULL,
    confidence_policy TEXT NOT NULL DEFAULT 'exact_or_high_confidence_fuzzy',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_file, source_rank, canonical_name)
);

CREATE INDEX IF NOT EXISTS idx_ref_13f_manager_style_norm
    ON ref_13f_manager_style (normalized_name);

CREATE TABLE IF NOT EXISTS core_13f_manager_classification (
    manager_cik TEXT NOT NULL REFERENCES core_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    primary_label TEXT,
    confidence_score NUMERIC(6,5),
    route_tier TEXT NOT NULL,
    route_reason TEXT,
    quantitative_trigger_metric TEXT,
    evidence_source TEXT,
    reference_id BIGINT,
    model TEXT,
    prompt_version TEXT,
    classification_status TEXT NOT NULL DEFAULT 'classified',
    response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_type TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period)
);

CREATE INDEX IF NOT EXISTS idx_core_13f_manager_classification_label
    ON core_13f_manager_classification (primary_label, confidence_score DESC);

CREATE TABLE IF NOT EXISTS recon_13f_period (
    report_period DATE PRIMARY KEY,
    dataset_count INTEGER NOT NULL DEFAULT 0,
    downloaded_count INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    standardized_count INTEGER NOT NULL DEFAULT 0,
    classified_managers INTEGER NOT NULL DEFAULT 0,
    filings INTEGER NOT NULL DEFAULT 0,
    latest_filings INTEGER NOT NULL DEFAULT 0,
    holdings BIGINT NOT NULL DEFAULT 0,
    managers INTEGER NOT NULL DEFAULT 0,
    summary_table_entries BIGINT,
    parsed_holding_rows BIGINT,
    summary_reported_value NUMERIC,
    parsed_reported_value NUMERIC,
    issuer_resolved_weight NUMERIC,
    price_coverage_weight NUMERIC,
    factor_coverage_weight NUMERIC,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
