-- Japan MOF official JGB constant-maturity yield curve.
--
-- Source CSV:
-- https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv

SET search_path TO sec, public;

INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key)
VALUES
    ('mof_jp', 'Japan Ministry of Finance', 'JP',
     'https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical',
     FALSE)
ON CONFLICT (source_id) DO UPDATE SET
    name = EXCLUDED.name,
    jurisdiction = EXCLUDED.jurisdiction,
    base_url = EXCLUDED.base_url,
    requires_api_key = EXCLUDED.requires_api_key;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('MOF_JP:JGB_1Y', 'mof_jp', 'jgbcme_all:1Y',
     'JGB 1Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_1y_yield'),
    ('MOF_JP:JGB_2Y', 'mof_jp', 'jgbcme_all:2Y',
     'JGB 2Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 1, 'jp_2y_yield'),
    ('MOF_JP:JGB_5Y', 'mof_jp', 'jgbcme_all:5Y',
     'JGB 5Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_5y_yield'),
    ('MOF_JP:JGB_10Y', 'mof_jp', 'jgbcme_all:10Y',
     'JGB 10Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 1, 'jp_10y_yield'),
    ('MOF_JP:JGB_20Y', 'mof_jp', 'jgbcme_all:20Y',
     'JGB 20Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_20y_yield'),
    ('MOF_JP:JGB_30Y', 'mof_jp', 'jgbcme_all:30Y',
     'JGB 30Y Constant Maturity Yield', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 2, 'jp_30y_yield')
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

-- Hide stale / incomplete BOJ/FRED JGB proxies from the visible slot layer.
UPDATE ref_macro_series
SET is_active = FALSE,
    story_tile_slot = NULL
WHERE series_id IN (
    'BOJ:JGB_1Y',
    'BOJ:JGB_2Y',
    'BOJ:JGB_5Y',
    'BOJ:JGB_10Y',
    'BOJ:JGB_20Y',
    'BOJ:JGB_30Y'
);
