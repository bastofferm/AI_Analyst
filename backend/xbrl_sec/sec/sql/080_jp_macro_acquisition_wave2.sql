-- JP macro acquisition wave 2: ESRI machinery orders, METI IIP, ESRI release calendar.
-- Idempotent; safe to re-run.

SET search_path TO sec, public;

INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key)
VALUES
    ('meti_jp', 'Japan Ministry of Economy, Trade and Industry', 'JP', 'https://www.meti.go.jp/english/statistics', FALSE)
ON CONFLICT (source_id) DO UPDATE SET
    name = EXCLUDED.name,
    jurisdiction = EXCLUDED.jurisdiction,
    base_url = EXCLUDED.base_url,
    requires_api_key = EXCLUDED.requires_api_key;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('CAO_JP:MACH_ORDERS', 'cao_jp', 'direct:machinery_orders_core_sa',
     'Core Machinery Orders, Private Sector ex Volatile Orders', 'activity', 'JP', 'M', 'Million JPY', TRUE, TRUE, 2, 'jp_machinery_orders'),
    ('CAO_JP:MACH_ORDERS_PRIVATE', 'cao_jp', 'direct:machinery_orders_private_sa',
     'Machinery Orders, Private Sector', 'activity', 'JP', 'M', 'Million JPY', TRUE, TRUE, 3, NULL),
    ('METI:JP_IIP_PRODUCTION_SA', 'meti_jp', 'current_html:production_sa',
     'Industrial Production Index, Production SA', 'activity', 'JP', 'M', 'Index 2020=100', TRUE, TRUE, 1, 'jp_iip'),
    ('METI:JP_IIP_SHIPMENTS_SA', 'meti_jp', 'current_html:shipments_sa',
     'Industrial Production Index, Shipments SA', 'activity', 'JP', 'M', 'Index 2020=100', TRUE, TRUE, 3, NULL),
    ('METI:JP_IIP_INVENTORIES_SA', 'meti_jp', 'current_html:inventories_sa',
     'Industrial Production Index, Inventories SA', 'activity', 'JP', 'M', 'Index 2020=100', TRUE, TRUE, 3, NULL),
    ('METI:JP_IIP_INVENTORY_RATIO_SA', 'meti_jp', 'current_html:inventory_ratio_sa',
     'Industrial Production Index, Inventory Ratio SA', 'activity', 'JP', 'M', 'Index 2020=100', TRUE, TRUE, 3, NULL)
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

-- METI is now the visible JP IIP owner. Keep the old BOJ/FRED mirror for
-- history only; it stopped in 2024 and should not compete for jp_iip.
UPDATE ref_macro_series
SET    is_active = FALSE,
       story_tile_slot = NULL
WHERE  series_id = 'BOJ:IIP';
