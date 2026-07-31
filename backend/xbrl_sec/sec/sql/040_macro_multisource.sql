-- Multi-source macro infrastructure.
--
-- Generalises the existing FRED-only fact_macro / ref_fred_series schema to
-- support BOJ, Cabinet Office (JP), BEA, BLS, ECB, SNB, RBA, MAS and HKMA.
-- Introduces release-vintage capture, bilingual LLM story storage, and a
-- business-cycle factor table.
--
-- Idempotent — safe to re-run.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Source registry
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_macro_source (
    source_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    jurisdiction     CHAR(2) NOT NULL,    -- e.g. 'US','JP','EZ','CH','AU','SG','HK','XX' (global/composite)
    base_url         TEXT,
    license          TEXT,
    requires_api_key BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_macro_source IS
    'Registry of macro data providers (FRED, BOJ, ECB, etc.). Drives ingest routing.';

INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key) VALUES
    ('fred',   'St. Louis Fed FRED',         'US', 'https://api.stlouisfed.org/fred',                          TRUE),
    ('bea',    'Bureau of Economic Analysis','US', 'https://apps.bea.gov/api/data',                            TRUE),
    ('bls',    'Bureau of Labor Statistics', 'US', 'https://api.bls.gov/publicAPI/v2',                         TRUE),
    ('boj',    'Bank of Japan',              'JP', 'https://www.stat-search.boj.or.jp',                        FALSE),
    ('cao_jp', 'JP Cabinet Office (ESRI)',   'JP', 'https://www.esri.cao.go.jp',                               FALSE),
    ('ecb',    'European Central Bank',      'EZ', 'https://data-api.ecb.europa.eu/service/data',              FALSE),
    ('snb',    'Swiss National Bank',        'CH', 'https://data.snb.ch/api/cube',                             FALSE),
    ('rba',    'Reserve Bank of Australia',  'AU', 'https://www.rba.gov.au/statistics/tables',                 FALSE),
    ('mas',    'Monetary Authority of Singapore', 'SG', 'https://eservices.mas.gov.sg/apimg-gw/server/msb-api',FALSE),
    ('hkma',   'Hong Kong Monetary Authority','HK','https://api.hkma.gov.hk/public/market-data-and-statistics',FALSE),
    ('oecd',   'OECD',                       'XX', 'https://sdmx.oecd.org/public/rest/data',                   FALSE),
    ('nyfed',  'NY Federal Reserve',         'US', 'https://www.newyorkfed.org',                               FALSE),
    ('atlfed', 'Atlanta Federal Reserve',    'US', 'https://www.atlantafed.org',                               FALSE),
    ('bis',    'Bank for International Settlements','XX','https://stats.bis.org/api/v2',                       FALSE),
    ('compute','Internal derived series',    'XX', NULL,                                                       FALSE)
ON CONFLICT (source_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Series registry (generalised replacement for ref_fred_series)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_macro_series (
    series_id        TEXT PRIMARY KEY,    -- global namespaced id, e.g. 'FRED:DGS10'
    source_id        TEXT NOT NULL REFERENCES ref_macro_source (source_id),
    native_id        TEXT NOT NULL,       -- id used by the source api (e.g. 'DGS10', 'IR01_O_N')
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,       -- 'rates','inflation','growth','labor','credit','money_supply','housing','volatility','fx','sentiment','activity'
    jurisdiction     CHAR(2) NOT NULL,    -- 'US','JP','EZ','CH','AU','SG','HK','XX'
    frequency        TEXT,                -- 'D','W','M','Q','A'
    units            TEXT,
    seasonal_adj     BOOLEAN,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    importance       SMALLINT NOT NULL DEFAULT 2, -- 1=headline,2=secondary,3=detail
    story_tile_slot  TEXT,                -- e.g. 'us_policy_rate','jp_10y_yield' (NULL if not surfaced)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ref_macro_series_source     ON ref_macro_series (source_id);
CREATE INDEX IF NOT EXISTS idx_ref_macro_series_juris      ON ref_macro_series (jurisdiction);
CREATE INDEX IF NOT EXISTS idx_ref_macro_series_tile_slot  ON ref_macro_series (story_tile_slot) WHERE story_tile_slot IS NOT NULL;

COMMENT ON TABLE ref_macro_series IS
    'Global registry of macro time series across all providers. Joins fact_macro to source/jurisdiction.';

-- ---------------------------------------------------------------------------
-- Namespace existing FRED series_ids in fact_macro and ref_fred_series.
-- Strategy: copy FRED rows into ref_macro_series with FRED: prefix, then
-- update fact_macro keys. The legacy ref_fred_series stays in place; the
-- FK on fact_macro is dropped and re-pointed at ref_macro_series.
-- ---------------------------------------------------------------------------

-- Copy FRED registry rows into ref_macro_series (idempotent).
INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
SELECT
    'FRED:' || series_id,
    'fred',
    series_id,
    name,
    category,
    'US',
    frequency,
    units,
    seasonal_adj,
    is_active,
    CASE
        WHEN series_id IN ('DFF','DGS10','CPIAUCSL','UNRATE','VIXCLS','BAMLH0A0HYM2','GDPC1','PAYEMS') THEN 1
        ELSE 2
    END,
    CASE series_id
        WHEN 'DFF'          THEN 'us_policy_rate'
        WHEN 'FEDFUNDS'     THEN 'us_policy_rate_m'
        WHEN 'DGS2'         THEN 'us_2y_yield'
        WHEN 'DGS10'        THEN 'us_10y_yield'
        WHEN 'DGS30'        THEN 'us_30y_yield'
        WHEN 'T10YIE'       THEN 'us_breakeven_10y'
        WHEN 'CPIAUCSL'     THEN 'us_cpi_yoy'
        WHEN 'CPILFESL'     THEN 'us_core_cpi_yoy'
        WHEN 'PCEPILFE'     THEN 'us_core_pce_yoy'
        WHEN 'GDPC1'        THEN 'us_real_gdp'
        WHEN 'UNRATE'       THEN 'us_unemployment'
        WHEN 'PAYEMS'       THEN 'us_nonfarm_payrolls'
        WHEN 'BAMLH0A0HYM2' THEN 'us_hy_spread'
        WHEN 'BAMLC0A0CM'   THEN 'us_ig_spread'
        WHEN 'M2SL'         THEN 'us_m2'
        WHEN 'VIXCLS'       THEN 'us_vix'
        WHEN 'DTWEXBGS'     THEN 'us_dxy'
        WHEN 'HOUST'        THEN 'us_housing_starts'
        WHEN 'CSUSHPISA'    THEN 'us_home_prices'
        ELSE NULL
    END
FROM ref_fred_series
ON CONFLICT (series_id) DO NOTHING;

-- Rewrite fact_macro to use namespaced ids (only un-prefixed rows).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM fact_macro WHERE series_id NOT LIKE '%:%' LIMIT 1) THEN
        -- Drop legacy FK to ref_fred_series so the update doesn't violate it.
        ALTER TABLE fact_macro DROP CONSTRAINT IF EXISTS fact_macro_series_id_fkey;
        ALTER TABLE fact_macro DROP CONSTRAINT IF EXISTS fact_macro_fred_series_id_fkey;

        UPDATE fact_macro
        SET    series_id = 'FRED:' || series_id
        WHERE  series_id NOT LIKE '%:%';
    END IF;
END $$;

-- Re-point fact_macro at ref_macro_series.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'fact_macro_series_macro_fkey'
    ) THEN
        ALTER TABLE fact_macro
            ADD CONSTRAINT fact_macro_series_macro_fkey
            FOREIGN KEY (series_id) REFERENCES ref_macro_series (series_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Release vintage capture
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_macro_release (
    series_id          TEXT NOT NULL REFERENCES ref_macro_series (series_id),
    release_at         TIMESTAMPTZ NOT NULL,
    period_end         DATE NOT NULL,
    value              DOUBLE PRECISION,
    vintage_id         TEXT,
    source_release_id  TEXT,
    PRIMARY KEY (series_id, release_at)
);
CREATE INDEX IF NOT EXISTS idx_macro_release_period ON fact_macro_release (series_id, period_end DESC);

COMMENT ON TABLE fact_macro_release IS
    'Release-time history: captures point-in-time vintages and the wall-clock release moment (drives release calendar).';

-- ---------------------------------------------------------------------------
-- LLM story storage (bilingual EN/DE)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_macro_story (
    story_id         BIGSERIAL PRIMARY KEY,
    scope            TEXT NOT NULL,                       -- 'tile' | 'essay' | 'curve' | 'regime'
    scope_key        TEXT NOT NULL,                       -- e.g. 'tile:us_policy_rate', 'essay:GLOBAL-2026-05-21-am'
    lang             CHAR(2) NOT NULL DEFAULT 'en',       -- 'en' | 'de'
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    model            TEXT,
    prompt_version   TEXT,
    input_hash       TEXT,
    text             TEXT NOT NULL,
    structured_json  JSONB,
    UNIQUE (scope, scope_key, lang)
);
CREATE INDEX IF NOT EXISTS idx_macro_story_lookup ON fact_macro_story (scope, scope_key, lang, generated_at DESC);

COMMENT ON TABLE fact_macro_story IS
    'Bilingual (en/de) LLM-generated macro narratives — tile captions, daily essays, curve blurbs.';

-- ---------------------------------------------------------------------------
-- Business cycle factor + regime
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fact_macro_factor (
    date          DATE NOT NULL,
    factor_id     TEXT NOT NULL,           -- 'us_cycle','jp_cycle','global_cycle','us_rate_surprise','usd_factor'
    value         DOUBLE PRECISION,
    percentile    DOUBLE PRECISION,        -- vs 10Y history
    regime_label  TEXT,                    -- 'Early-expansion','Mid-expansion','Late-cycle','Contraction'
    top_loadings  JSONB,                   -- [{series:'CFNAI', loading:0.42}, ...]
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, factor_id)
);
CREATE INDEX IF NOT EXISTS idx_macro_factor_factor ON fact_macro_factor (factor_id, date DESC);

COMMENT ON TABLE fact_macro_factor IS
    'PCA business-cycle factors and regime classifications. Daily snapshots.';

-- ---------------------------------------------------------------------------
-- Seed series for new sources (BOJ + Cabinet Office in Wave 1)
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    -- BOJ
    ('BOJ:IR01_OCRT',  'boj', 'IR01''MUTKCALMUNCURE', 'Uncollateralised Overnight Call Rate', 'rates', 'JP', 'D', 'Percent', FALSE, 1, 'jp_policy_rate'),
    ('BOJ:JGB_1Y',     'boj', 'IR01''ST''SR01000''101N0AC1', 'JGB 1Y Yield',  'rates', 'JP', 'D', 'Percent', FALSE, 2, 'jp_1y_yield'),
    ('BOJ:JGB_2Y',     'boj', 'IR01''ST''SR01000''101N0AC2', 'JGB 2Y Yield',  'rates', 'JP', 'D', 'Percent', FALSE, 1, 'jp_2y_yield'),
    ('BOJ:JGB_5Y',     'boj', 'IR01''ST''SR01000''101N0AC5', 'JGB 5Y Yield',  'rates', 'JP', 'D', 'Percent', FALSE, 2, 'jp_5y_yield'),
    ('BOJ:JGB_10Y',    'boj', 'IR01''ST''SR01000''101N0AC10','JGB 10Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, 1, 'jp_10y_yield'),
    ('BOJ:JGB_20Y',    'boj', 'IR01''ST''SR01000''101N0AC20','JGB 20Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, 2, 'jp_20y_yield'),
    ('BOJ:JGB_30Y',    'boj', 'IR01''ST''SR01000''101N0AC30','JGB 30Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, 2, 'jp_30y_yield'),
    ('BOJ:TANKAN_LMFG','boj', 'CO''CO''CO01''COBQA01','Tankan Large Mfg Business Conditions DI','sentiment','JP','Q','DI Points',FALSE,1,'jp_tankan_lmfg'),
    ('BOJ:TANKAN_LNMFG','boj','CO''CO''CO01''COBQB01','Tankan Large Non-Mfg Business Conditions DI','sentiment','JP','Q','DI Points',FALSE,2,'jp_tankan_lnmfg'),
    ('BOJ:MB_AVG',     'boj', 'MD''MD02''MABS1AN11','Monetary Base (Average)', 'money_supply','JP','M','Billions JPY',FALSE,2,'jp_monetary_base'),
    ('BOJ:M2_AVG',     'boj', 'MD''MD01''MAM1NAM26','M2 Money Stock (Average)','money_supply','JP','M','Trillions JPY',FALSE,2,'jp_m2'),
    ('BOJ:CPI_EXFOOD', 'boj', 'PR01''PRCG15_EXFF',  'CPI ex Fresh Food (YoY)', 'inflation','JP','M','Percent',TRUE,1,'jp_cpi_yoy'),
    ('BOJ:CPI_CORE',   'boj', 'PR01''PRCG15_EXFFENG','CPI ex Fresh Food & Energy (YoY)','inflation','JP','M','Percent',TRUE,2,'jp_core_cpi_yoy'),
    ('BOJ:IIP',        'boj', 'OS''OS02''OSCB000010','Industrial Production Index','activity','JP','M','Index 2020=100',TRUE,2,'jp_iip'),
    -- Cabinet Office
    ('CAO_JP:CI_COIN', 'cao_jp','coincident', 'Coincident Composite Index','activity','JP','M','Index 2020=100',TRUE,1,'jp_ci_coincident'),
    ('CAO_JP:CI_LEAD', 'cao_jp','leading',    'Leading Composite Index',   'activity','JP','M','Index 2020=100',TRUE,1,'jp_ci_leading'),
    ('CAO_JP:CI_LAG',  'cao_jp','lagging',    'Lagging Composite Index',   'activity','JP','M','Index 2020=100',TRUE,3,'jp_ci_lagging'),
    ('CAO_JP:GDP_REAL_QOQ','cao_jp','gdp_real_qoq','Real GDP QoQ (annualised)','growth','JP','Q','Percent',TRUE,1,'jp_real_gdp_qoq'),
    ('CAO_JP:CONS_CONF','cao_jp','consumer_confidence','Consumer Confidence Index','sentiment','JP','M','Index',TRUE,2,'jp_consumer_conf'),
    ('CAO_JP:MACH_ORDERS','cao_jp','core_machinery_orders','Core Machinery Orders (Private ex-volatile)','activity','JP','M','Billions JPY',TRUE,2,'jp_machinery_orders')
ON CONFLICT (series_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Convenience view: latest value per series_id with jurisdiction/source.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_macro_latest AS
WITH ranked AS (
    SELECT  f.series_id, f.date, f.value,
            ROW_NUMBER() OVER (PARTITION BY f.series_id ORDER BY f.date DESC) AS rn
    FROM    fact_macro f
)
SELECT  r.series_id, s.source_id, s.jurisdiction, s.category, s.story_tile_slot,
        s.name, s.units, s.frequency, r.date AS latest_date, r.value AS latest_value
FROM    ranked r
JOIN    ref_macro_series s ON s.series_id = r.series_id
WHERE   r.rn = 1;

COMMENT ON VIEW v_macro_latest IS 'Latest observation per series_id with descriptive metadata. Powers /api/macro/signals.';
