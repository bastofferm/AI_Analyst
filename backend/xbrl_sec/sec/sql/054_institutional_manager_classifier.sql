SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_13f_manager_classification_override (
    manager_cik TEXT PRIMARY KEY REFERENCES dim_13f_manager(manager_cik),
    primary_label TEXT NOT NULL CHECK (primary_label IN (
        'Asset Management: Alternative (Speculative/Trading)',
        'Asset Management: Traditional (Long-Term Capital)',
        'Banking: Wealth & Trust (Investment)',
        'Banking: Capital Markets & Trading (Speculative)',
        'Insurance: General Account (Long-Term Capital)'
    )),
    confidence_score NUMERIC(6,5) NOT NULL DEFAULT 1.0,
    quantitative_trigger_metric TEXT,
    route_reason TEXT,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ref_13f_manager_style_reference (
    reference_id BIGSERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    source_category TEXT NOT NULL,
    source_rank INTEGER,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    domicile_or_headquarters TEXT,
    strategy_or_profile TEXT,
    target_label TEXT NOT NULL CHECK (target_label IN (
        'Asset Management: Alternative (Speculative/Trading)',
        'Asset Management: Traditional (Long-Term Capital)',
        'Banking: Wealth & Trust (Investment)',
        'Banking: Capital Markets & Trading (Speculative)',
        'Insurance: General Account (Long-Term Capital)'
    )),
    confidence_policy TEXT NOT NULL DEFAULT 'exact_or_high_confidence_fuzzy',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_file, source_rank, canonical_name)
);

CREATE INDEX IF NOT EXISTS idx_ref_13f_manager_style_reference_norm
    ON ref_13f_manager_style_reference (normalized_name);

CREATE INDEX IF NOT EXISTS idx_ref_13f_manager_style_reference_label
    ON ref_13f_manager_style_reference (target_label);

CREATE TABLE IF NOT EXISTS ref_13f_manager_entity_match (
    manager_cik TEXT NOT NULL,
    reference_id BIGINT,
    manager_name TEXT NOT NULL,
    reference_name TEXT,
    target_label TEXT CHECK (
        target_label IS NULL OR target_label IN (
            'Asset Management: Alternative (Speculative/Trading)',
            'Asset Management: Traditional (Long-Term Capital)',
            'Banking: Wealth & Trust (Investment)',
            'Banking: Capital Markets & Trading (Speculative)',
            'Insurance: General Account (Long-Term Capital)'
        )
    ),
    match_type TEXT NOT NULL,
    match_score NUMERIC(6,5),
    matched_name TEXT,
    evidence_source TEXT,
    conflict_reason TEXT,
    status TEXT NOT NULL DEFAULT 'matched' CHECK (status IN ('matched', 'conflict', 'unmatched')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik)
);

CREATE INDEX IF NOT EXISTS idx_ref_13f_manager_entity_match_reference
    ON ref_13f_manager_entity_match (reference_id);

CREATE INDEX IF NOT EXISTS idx_ref_13f_manager_entity_match_label
    ON ref_13f_manager_entity_match (target_label, status, match_score DESC);

CREATE TABLE IF NOT EXISTS fact_13f_manager_feature_snapshot (
    manager_cik TEXT NOT NULL REFERENCES dim_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    input_hash TEXT NOT NULL,
    legal_name TEXT NOT NULL,
    current_aum NUMERIC,
    median_turnover_rate NUMERIC,
    max_turnover_rate NUMERIC,
    median_options_ratio NUMERIC,
    mean_position_count NUMERIC,
    top_5_concentration NUMERIC,
    consecutive_quarters INTEGER,
    shares_voting_sole_pct NUMERIC,
    price_coverage_weight NUMERIC,
    feature_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_manager_feature_snapshot_latest
    ON fact_13f_manager_feature_snapshot (manager_cik, report_period DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS fact_13f_manager_classification (
    manager_cik TEXT NOT NULL REFERENCES dim_13f_manager(manager_cik),
    report_period DATE NOT NULL,
    input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    classification_status TEXT NOT NULL DEFAULT 'classified',
    primary_label TEXT CHECK (
        primary_label IS NULL OR primary_label IN (
            'Asset Management: Alternative (Speculative/Trading)',
            'Asset Management: Traditional (Long-Term Capital)',
            'Banking: Wealth & Trust (Investment)',
            'Banking: Capital Markets & Trading (Speculative)',
            'Insurance: General Account (Long-Term Capital)'
        )
    ),
    confidence_score NUMERIC(6,5),
    quantitative_trigger_metric TEXT,
    route_tier TEXT NOT NULL,
    route_reason TEXT,
    evidence_source TEXT,
    model TEXT,
    response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_type TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period, input_hash, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_fact_13f_manager_classification_latest
    ON fact_13f_manager_classification (manager_cik, report_period DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_manager_classification_label
    ON fact_13f_manager_classification (primary_label, confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_fact_13f_manager_classification_route
    ON fact_13f_manager_classification (route_tier, classification_status);
