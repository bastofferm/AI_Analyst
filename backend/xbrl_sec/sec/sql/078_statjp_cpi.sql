-- Official Statistics Japan CPI series via DBnomics.

SET search_path TO sec, public;

INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key)
VALUES
    ('statjp', 'Statistics Japan via DBnomics', 'JP',
     'https://api.db.nomics.world/v22/series/STATJP',
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
    ('STATJP:CPI_ALL', 'statjp', 'CPIm:001',
     'CPI All Items', 'inflation', 'JP', 'M', 'Index 2020=100', FALSE, TRUE, 2, 'jp_headline_cpi'),
    ('STATJP:CPI_EX_FRESH', 'statjp', 'CPIm:733',
     'CPI All Items Less Fresh Food', 'inflation', 'JP', 'M', 'Index 2020=100', FALSE, TRUE, 1, 'jp_cpi_yoy'),
    ('STATJP:CPI_EX_FRESH_ENERGY', 'statjp', 'CPIm:740',
     'CPI All Items Less Fresh Food and Energy', 'inflation', 'JP', 'M', 'Index 2020=100', FALSE, TRUE, 2, 'jp_core_cpi_yoy')
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

-- Hide stale / empty CPI placeholders from the visible slot layer.
UPDATE ref_macro_series
SET is_active = FALSE,
    story_tile_slot = NULL
WHERE series_id IN (
    'BOJ:CPI_CORE',
    'BOJ:CPI_EXFOOD',
    'BOJ:CPI_YOY',
    'BOJ:CPI_INDEX',
    'BOJ:CPI_ALL'
);
