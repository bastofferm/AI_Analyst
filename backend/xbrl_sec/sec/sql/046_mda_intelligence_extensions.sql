-- MD&A keyword and summary sidecar layer.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_mda_keyword_lexicon (
    keyword_id SMALLSERIAL PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK (category IN ('risk','opportunity','neutral')),
    subcategory TEXT,
    regex_pattern TEXT NOT NULL,
    case_insensitive BOOLEAN NOT NULL DEFAULT true,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_mda_keyword_hits (
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    entity_id TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    keyword_id SMALLINT NOT NULL REFERENCES ref_mda_keyword_lexicon(keyword_id),
    match_count INTEGER NOT NULL DEFAULT 0,
    context_snippets TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    scored_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, entity_id, filing_id, section_id, keyword_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_mda_keyword_hits_entity
    ON fact_mda_keyword_hits (jurisdiction, entity_id, filing_id);

CREATE TABLE IF NOT EXISTS fact_mda_summary_cache (
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    entity_id TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    model TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    risk_factors TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    opportunities TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    outlook TEXT,
    source_chars INTEGER,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, entity_id, filing_id, section_id, model)
);

INSERT INTO ref_mda_keyword_lexicon
    (keyword, category, subcategory, regex_pattern, description)
VALUES
    ('supply_chain_risk', 'risk', 'operational', 'supply\s+chain\s+(disruption|constraint|shortage|risk|bottleneck|dependency)', 'Supply-chain disruption or dependency language.'),
    ('export_controls', 'risk', 'regulatory', 'export\s+(control|restriction|licens\w+|ban|curb)', 'Export-control and cross-border regulatory risk.'),
    ('inflation_pressure', 'risk', 'macroeconomic', 'inflation\w*\s+(pressure|impact|headwind|cost|increase|risk)', 'Inflation cost pressure or margin headwind language.'),
    ('liquidity_risk', 'risk', 'financial', '(liquidity|cash)\s+(risk|constraint|pressure|shortfall)', 'Liquidity or cash availability risk.'),
    ('restructuring', 'risk', 'operational', 'restructur\w+|workforce\s+reduction|cost\s+reduction\s+program', 'Restructuring and cost-reduction discussion.'),
    ('ai_demand_growth', 'opportunity', 'technology', '(AI|artificial intelligence|machine learning)\s+(demand|growth|opportunit|adoption|ramp)', 'AI-related demand or growth language.'),
    ('data_center_expansion', 'opportunity', 'infrastructure', 'data\s+center\s+(growth|expansion|ramp|scale|build.?out|investment)', 'Data-center growth or capacity expansion.'),
    ('margin_expansion', 'opportunity', 'profitability', 'margin\s+(expansion|improvement|leverage|benefit)', 'Margin improvement language.'),
    ('market_share_gain', 'opportunity', 'competitive', 'market\s+share\s+(gain|growth|increase|expansion)', 'Market-share gain or competitive improvement.'),
    ('capital_return', 'neutral', 'capital_allocation', '(share\s+repurchase|buyback|dividend|return\s+capital)', 'Capital return language.')
ON CONFLICT (keyword) DO UPDATE SET
    category = EXCLUDED.category,
    subcategory = EXCLUDED.subcategory,
    regex_pattern = EXCLUDED.regex_pattern,
    description = EXCLUDED.description,
    updated_at = now();
