-- Debt topic seeds for the macro workspace.
--
-- Adds public-debt and household-debt series as first-class macro signals.
-- The /macro redesign groups category='debt' alongside credit spreads under
-- the Debt topic. This migration only registers verified FRED-backed rows;
-- private-debt coverage can be broadened later as source coverage is validated.
--
-- Idempotent - safe to re-run.

SET search_path TO sec, public;

-- Keep the legacy FRED whitelist in sync so xbrl_sec.sec.sources.fred_ingest
-- can fetch the underlying observations and write namespaced FRED:* facts.
INSERT INTO ref_fred_series
    (series_id, name, category, frequency, units, seasonal_adj, is_active)
VALUES
    ('GFDEGDQ188S',
     'Federal Debt: Total Public Debt as Percent of Gross Domestic Product',
     'debt', 'Q', 'Percent of GDP', FALSE, TRUE),
    ('JPNGGXWDGG01GDPPT',
     'General Government Gross Debt for Japan',
     'debt', 'A', 'Percent of GDP', FALSE, TRUE),
    ('GGGDTAEZA188N',
     'General Government Gross Debt for Euro Area',
     'debt', 'A', 'Percent of GDP', FALSE, TRUE),
    ('HDTGPDUSQ163N',
     'Household Debt to GDP for United States',
     'debt', 'Q', 'Percent of GDP', FALSE, TRUE)
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
    ('FRED:GFDEGDQ188S', 'fred', 'GFDEGDQ188S',
     'US Federal Debt / GDP',
     'debt', 'US', 'Q', 'Percent of GDP', FALSE, TRUE, 1, 'us_public_debt_gdp'),
    ('FRED:JPNGGXWDGG01GDPPT', 'fred', 'JPNGGXWDGG01GDPPT',
     'Japan General Government Gross Debt / GDP',
     'debt', 'JP', 'A', 'Percent of GDP', FALSE, TRUE, 1, 'jp_public_debt_gdp'),
    ('FRED:GGGDTAEZA188N', 'fred', 'GGGDTAEZA188N',
     'Euro Area General Government Gross Debt / GDP',
     'debt', 'EZ', 'A', 'Percent of GDP', FALSE, TRUE, 1, 'ez_public_debt_gdp'),
    ('FRED:HDTGPDUSQ163N', 'fred', 'HDTGPDUSQ163N',
     'US Household Debt / GDP',
     'debt', 'US', 'Q', 'Percent of GDP', FALSE, TRUE, 2, 'us_household_debt_gdp')
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
