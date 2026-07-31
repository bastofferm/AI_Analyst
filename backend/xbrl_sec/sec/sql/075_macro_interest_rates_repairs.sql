-- Interest-rates topic data repairs and market-rate expansion.
--
-- Makes existing US tenor facts visible, moves credit spreads out of the
-- Debt topic by giving them first-class rate-topic slots, registers official
-- JP/EZ curve tenors, and disables stale duplicate story slots.
--
-- Idempotent - safe to re-run.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- US Treasury tenors that already have FRED facts but were not surfaced.
-- ---------------------------------------------------------------------------

UPDATE ref_macro_series
SET story_tile_slot = 'us_1m_yield',
    importance = 2,
    is_active = TRUE
WHERE series_id = 'FRED:DGS1MO';

UPDATE ref_macro_series
SET story_tile_slot = 'us_6m_yield',
    importance = 2,
    is_active = TRUE
WHERE series_id = 'FRED:DGS6MO';

UPDATE ref_macro_series
SET story_tile_slot = 'us_1y_yield',
    importance = 2,
    is_active = TRUE
WHERE series_id = 'FRED:DGS1';

-- ---------------------------------------------------------------------------
-- US credit spread rating grades. These are ICE BofA OAS series mirrored by
-- FRED and belong in the Interest Rates / market spread workspace.
-- ---------------------------------------------------------------------------

INSERT INTO ref_fred_series
    (series_id, name, category, frequency, units, seasonal_adj, is_active)
VALUES
    ('BAMLC0A1CAAA', 'ICE BofA AAA US Corporate Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLC0A2CAA',  'ICE BofA AA US Corporate Index Option-Adjusted Spread',  'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLC0A3CA',   'ICE BofA Single-A US Corporate Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLC0A4CBBB', 'ICE BofA BBB US Corporate Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLH0A1HYBB', 'ICE BofA BB US High Yield Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLH0A2HYB',  'ICE BofA Single-B US High Yield Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE),
    ('BAMLH0A3HYC',  'ICE BofA CCC & Lower US High Yield Index Option-Adjusted Spread', 'credit', 'D', 'Percent', FALSE, TRUE)
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
    ('FRED:BAMLC0A1CAAA', 'fred', 'BAMLC0A1CAAA',
     'ICE BofA AAA US Corporate OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 3, 'us_aaa_spread'),
    ('FRED:BAMLC0A2CAA', 'fred', 'BAMLC0A2CAA',
     'ICE BofA AA US Corporate OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 3, 'us_aa_spread'),
    ('FRED:BAMLC0A3CA', 'fred', 'BAMLC0A3CA',
     'ICE BofA A US Corporate OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 3, 'us_a_spread'),
    ('FRED:BAMLC0A4CBBB', 'fred', 'BAMLC0A4CBBB',
     'ICE BofA BBB US Corporate OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_bbb_spread'),
    ('FRED:BAMLH0A1HYBB', 'fred', 'BAMLH0A1HYBB',
     'ICE BofA BB US High Yield OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_bb_spread'),
    ('FRED:BAMLH0A2HYB', 'fred', 'BAMLH0A2HYB',
     'ICE BofA B US High Yield OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 3, 'us_b_spread'),
    ('FRED:BAMLH0A3HYC', 'fred', 'BAMLH0A3HYC',
     'ICE BofA CCC & Lower US High Yield OAS', 'credit', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_ccc_spread')
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

UPDATE ref_macro_series
SET story_tile_slot = 'us_3m_ff_spread',
    importance = 3,
    is_active = TRUE
WHERE series_id = 'FRED:T3MFF';

-- ---------------------------------------------------------------------------
-- JP official curve registry. 10Y keeps the existing FRED-backed proxy row
-- because it already has history; other tenors use BOJ Time-Series codes.
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('BOJ:IR01_OCRT', 'boj', 'IR01''MUTKCALMUNCURE',
     'Uncollateralised Overnight Call Rate', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 1, 'jp_call_rate'),
    ('BOJ:JGB_1Y', 'boj', 'IR01''ST''SR01000''101N0AC1',
     'JGB 1Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_1y_yield'),
    ('BOJ:JGB_2Y', 'boj', 'IR01''ST''SR01000''101N0AC2',
     'JGB 2Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 1, 'jp_2y_yield'),
    ('BOJ:JGB_5Y', 'boj', 'IR01''ST''SR01000''101N0AC5',
     'JGB 5Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_5y_yield'),
    ('BOJ:JGB_20Y', 'boj', 'IR01''ST''SR01000''101N0AC20',
     'JGB 20Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_20y_yield'),
    ('BOJ:JGB_30Y', 'boj', 'IR01''ST''SR01000''101N0AC30',
     'JGB 30Y Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_30y_yield')
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

-- Official BOJ rows for stale FRED money/CPI proxies. These will show an
-- honest "registered, not fetched" state until the BOJ endpoint returns CSV.
INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('BOJ:MB_AVG', 'boj', 'MD''MD02''MABS1AN11',
     'Monetary Base (Average)', 'money_supply', 'JP', 'M', 'Billions JPY', FALSE, TRUE, 2, 'jp_monetary_base'),
    ('BOJ:M2_AVG', 'boj', 'MD''MD01''MAM1NAM26',
     'M2 Money Stock (Average)', 'money_supply', 'JP', 'M', 'Trillions JPY', FALSE, TRUE, 2, 'jp_m2'),
    ('BOJ:CPI_EXFOOD', 'boj', 'PR01''PRCG15_EXFF',
     'CPI ex Fresh Food YoY', 'inflation', 'JP', 'M', 'Percent', TRUE, TRUE, 1, 'jp_cpi_yoy'),
    ('BOJ:CPI_CORE', 'boj', 'PR01''PRCG15_EXFFENG',
     'CPI ex Fresh Food & Energy YoY', 'inflation', 'JP', 'M', 'Percent', TRUE, TRUE, 2, 'jp_core_cpi_yoy')
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
-- EZ official ECB AAA government yield-curve tenors.
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('ECB:BUND_1Y', 'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_1Y',
     'EA 1Y AAA Government Bond Yield', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_1y_yield'),
    ('ECB:BUND_2Y', 'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y',
     'EA 2Y AAA Government Bond Yield', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 1, 'ez_2y_yield'),
    ('ECB:BUND_5Y', 'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y',
     'EA 5Y AAA Government Bond Yield', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_5y_yield'),
    ('ECB:BUND_20Y', 'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_20Y',
     'EA 20Y AAA Government Bond Yield', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_20y_yield'),
    ('ECB:BUND_30Y', 'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_30Y',
     'EA 30Y AAA Government Bond Yield', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_30y_yield')
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

UPDATE ref_macro_series
SET is_active = TRUE,
    story_tile_slot = 'ez_10y_yield',
    importance = 1
WHERE series_id = 'ECB:BUND_10Y';

-- Derived slope placeholders populated by macro_compute_derived.py.
INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('COMPUTE:JP_2S10S', 'compute', 'jp_2s10s',
     'JGB 10Y-2Y Yield Spread', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_2s10s'),
    ('COMPUTE:EZ_2S10S', 'compute', 'ez_2s10s',
     'EA AAA 10Y-2Y Yield Spread', 'rates', 'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_2s10s')
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
-- Duplicate slot cleanup. Keep the rows with the freshest usable observations.
-- ---------------------------------------------------------------------------

UPDATE ref_macro_series
SET is_active = FALSE,
    story_tile_slot = NULL
WHERE series_id IN (
    'ECB:MRR',
    'ECB:RATE_10Y',
    'ECB:UNRATE',
    'SNB:CONF_10Y',
    'FRED:ADSINDEX',
    'FRED:GDPNOW',
    'FRED:USSLIND',
    'BOJ:POLICY_RATE',
    'BOJ:CALL_RATE',
    'BOJ:M2',
    'BOJ:MONETARY_BASE',
    'BOJ:CPI_YOY',
    'BOJ:CPI_INDEX'
);
