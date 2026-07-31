-- BOJ Stat-Search API mappings for official JP macro series.
--
-- The BOJ Web API uses database IDs plus SERIES_CODE values, not the legacy
-- UI-native IDs used by the old famecgi2 path. These mappings were verified
-- against https://www.stat-search.boj.or.jp/api/v1/getMetadata.

SET search_path TO sec, public;

UPDATE ref_macro_series
SET native_id = 'api:FM01:STRDCLUCON',
    name = 'Call Rate, Uncollateralized Overnight, Average',
    category = 'rates',
    jurisdiction = 'JP',
    frequency = 'D',
    units = 'Percent',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 1,
    story_tile_slot = 'jp_call_rate'
WHERE series_id = 'BOJ:IR01_OCRT';

UPDATE ref_macro_series
SET native_id = 'api:MD01:MABS1AN11',
    name = 'Monetary Base, Average Amounts Outstanding',
    category = 'money_supply',
    jurisdiction = 'JP',
    frequency = 'M',
    units = '100 million JPY',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 2,
    story_tile_slot = 'jp_monetary_base'
WHERE series_id = 'BOJ:MB_AVG';

UPDATE ref_macro_series
SET native_id = 'api:MD01:MABS1AN113',
    name = 'BOJ Current Account Balances, Average Amounts Outstanding',
    category = 'liquidity',
    jurisdiction = 'JP',
    frequency = 'M',
    units = '100 million JPY',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 2,
    story_tile_slot = 'jp_boj_current_account'
WHERE series_id = 'BOJ:CA_BAL';

UPDATE ref_macro_series
SET native_id = 'api:MD02:MAM1NAM2M2MO',
    name = 'M2, Average Amounts Outstanding',
    category = 'money_supply',
    jurisdiction = 'JP',
    frequency = 'M',
    units = '100 million JPY',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 2,
    story_tile_slot = 'jp_m2'
WHERE series_id = 'BOJ:M2_AVG';

UPDATE ref_macro_series
SET native_id = 'api:CO:TK99F1000601GCQ01000',
    name = 'Tankan Business Conditions DI, Large Enterprises Manufacturing',
    category = 'sentiment',
    jurisdiction = 'JP',
    frequency = 'Q',
    units = 'DI Points',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 1,
    story_tile_slot = 'jp_tankan_lmfg'
WHERE series_id = 'BOJ:TANKAN_LMFG';

UPDATE ref_macro_series
SET native_id = 'api:CO:TK99F2000601GCQ01000',
    name = 'Tankan Business Conditions DI, Large Enterprises Nonmanufacturing',
    category = 'sentiment',
    jurisdiction = 'JP',
    frequency = 'Q',
    units = 'DI Points',
    seasonal_adj = FALSE,
    is_active = TRUE,
    importance = 2,
    story_tile_slot = 'jp_tankan_lnmfg'
WHERE series_id = 'BOJ:TANKAN_LNMFG';

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('BOJ:TANKAN_LALL', 'boj', 'api:CO:TK99F0000601GCQ01000',
     'Tankan Business Conditions DI, Large Enterprises All Industries',
     'sentiment', 'JP', 'Q', 'DI Points', FALSE, TRUE, 3, 'jp_tankan_lall')
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
