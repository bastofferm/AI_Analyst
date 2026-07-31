-- Unified macro business-cycle assessment.
--
-- Adds state-probability inputs, visible normalized/proxy outputs, and a
-- cached regional cycle-assessment table. Idempotent - safe to re-run.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Source probability / proxy inputs
-- ---------------------------------------------------------------------------

INSERT INTO ref_fred_series
    (series_id, name, category, frequency, units, seasonal_adj, is_active)
VALUES
    ('RECPROUSM156N',
     'Smoothed U.S. Recession Probabilities',
     'state_probability', 'M', 'Percent', FALSE, TRUE),
    ('JHGDPBRINDX',
     'GDP-Based Recession Indicator Index',
     'state_probability', 'Q', 'Percentage Points', FALSE, TRUE)
ON CONFLICT (series_id) DO UPDATE SET
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    frequency = EXCLUDED.frequency,
    units = EXCLUDED.units,
    seasonal_adj = EXCLUDED.seasonal_adj,
    is_active = EXCLUDED.is_active;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('FRED:RECPROUSM156N', 'fred', 'RECPROUSM156N',
     'US Smoothed Recession Probability (MS-DFM)',
     'state_probability', 'US', 'M', 'Percent', FALSE, TRUE, 1, NULL),
    ('FRED:JHGDPBRINDX', 'fred', 'JHGDPBRINDX',
     'US GDP-Based Recession Probability (Hamilton)',
     'state_probability', 'US', 'Q', 'Percentage Points', FALSE, TRUE, 2, NULL),
    ('NYFED:YC_RECESSION_12M_RAW', 'nyfed', 'yc_recession_12m',
     'NY Fed 12M Recession Probability (Yield Curve)',
     'state_probability', 'US', 'M', 'Probability (0-1)', FALSE, TRUE, 2, NULL),
    ('ECB:CISS_EA_NEW', 'ecb', 'CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX',
     'ECB New CISS - Euro Area State-Risk Proxy',
     'financial_stress', 'EZ', 'D', 'Probability (0-1)', FALSE, TRUE, 2, NULL)
ON CONFLICT (series_id) DO UPDATE SET
    source_id = EXCLUDED.source_id,
    native_id = EXCLUDED.native_id,
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    jurisdiction = EXCLUDED.jurisdiction,
    frequency = EXCLUDED.frequency,
    units = EXCLUDED.units,
    seasonal_adj = EXCLUDED.seasonal_adj,
    is_active = EXCLUDED.is_active,
    importance = EXCLUDED.importance,
    story_tile_slot = EXCLUDED.story_tile_slot;

-- ---------------------------------------------------------------------------
-- Visible normalized/proxy state rows consumed by the assessment and UI.
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('COMPUTE:US_RECESSION_PROB_MS_DFM', 'compute', 'us_recession_prob_ms_dfm',
     'US Recession Probability (MS-DFM)',
     'state_probability', 'US', 'M', 'Probability (0-1)', FALSE, TRUE, 1, 'us_recession_prob_ms_dfm'),
    ('COMPUTE:US_RECESSION_PROB_GDP_HAMILTON', 'compute', 'us_recession_prob_gdp_hamilton',
     'US GDP Recession Probability (Hamilton)',
     'state_probability', 'US', 'Q', 'Probability (0-1)', FALSE, TRUE, 2, 'us_recession_prob_gdp_hamilton'),
    ('COMPUTE:US_RECESSION_PROB_12M_NYFED', 'compute', 'us_recession_prob_12m_nyfed',
     'US 12M Recession Probability (NY Fed Yield Curve)',
     'state_probability', 'US', 'M', 'Probability (0-1)', FALSE, TRUE, 2, 'us_recession_prob_12m_nyfed'),
    ('COMPUTE:EZ_STATE_RISK_CISS', 'compute', 'ez_state_risk_ciss',
     'Eurozone State-Risk Proxy (ECB CISS)',
     'state_proxy', 'EZ', 'M', 'Probability (0-1)', FALSE, TRUE, 1, 'ez_state_risk_ciss'),
    ('COMPUTE:JP_CI_RECESSION_PROXY', 'compute', 'jp_ci_recession_proxy',
     'Japan Recession-State Proxy (Cabinet Office CI)',
     'state_proxy', 'JP', 'M', 'Probability (0-1)', FALSE, TRUE, 1, 'jp_ci_recession_proxy')
ON CONFLICT (series_id) DO UPDATE SET
    source_id = EXCLUDED.source_id,
    native_id = EXCLUDED.native_id,
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    jurisdiction = EXCLUDED.jurisdiction,
    frequency = EXCLUDED.frequency,
    units = EXCLUDED.units,
    seasonal_adj = EXCLUDED.seasonal_adj,
    is_active = EXCLUDED.is_active,
    importance = EXCLUDED.importance,
    story_tile_slot = EXCLUDED.story_tile_slot;

-- ---------------------------------------------------------------------------
-- Cached regional synthesis.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_macro_cycle_assessment (
    jurisdiction            CHAR(6) NOT NULL,
    period_end              DATE NOT NULL,
    phase                   TEXT NOT NULL CHECK (phase IN (
                                'expansion',
                                'late_cycle',
                                'slowdown',
                                'contraction',
                                'recovery',
                                'mixed'
                            )),
    score                   NUMERIC(6,2),
    recession_probability   NUMERIC(8,6),
    confidence              NUMERIC(6,4),
    drivers_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, period_end)
);

CREATE INDEX IF NOT EXISTS idx_macro_cycle_current
    ON fact_macro_cycle_assessment (jurisdiction, period_end DESC);

COMMENT ON TABLE fact_macro_cycle_assessment IS
    'Cached regional macro cycle synthesis across liquidity, rates, inflation, growth, debt/credit and labor.';
