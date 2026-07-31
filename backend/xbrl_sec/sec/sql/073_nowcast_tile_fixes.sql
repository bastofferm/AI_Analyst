-- Nowcast tile fixes (post-launch screenshots revealed gaps).
--
-- 1. Register 'phillyfed' as a macro source.
-- 2. Replace the broken FRED:ADSINDEX (no such FRED ID) with PHILLYFED:ADS
--    sourced directly from philadelphiafed.org. Deactivate the broken row.
-- 3. Replace FRED:USSLIND (state-level US LEI, stale at 2020-02) with the
--    OECD Composite Leading Indicator for the US, served via FRED at
--    USALOLITOAASTSAM (monthly, current to 2026-04).
-- 4. Deactivate NYFED:NOWCAST — the NY Fed discontinued the Staff Nowcast in
--    Sep 2024; the available data caps at 2021-08 and showing a stale tile
--    is misleading.
-- 5. Deactivate ECB:NOWCAST_GDP — ECB does not expose nowcasts via SDMX,
--    no FRED mirror exists; users should drop a CSV into
--    D:/macroData/drops/bde/eurosting.csv or wait for a future direct
--    ingest.
--
-- Idempotent — safe to re-run.

SET search_path TO sec, public;

-- 1. New source
INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key) VALUES
    ('phillyfed', 'Philadelphia Federal Reserve', 'US', 'https://www.philadelphiafed.org', FALSE)
ON CONFLICT (source_id) DO NOTHING;

-- 2. ADS Index — switch to Philly Fed direct
INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    ('PHILLYFED:ADS', 'phillyfed', 'direct:ads',
     'Aruoba-Diebold-Scotti Business Conditions Index',
     'nowcast', 'US', 'D', 'Std. units (z-score)', FALSE, 2, 'us_ads')
ON CONFLICT (series_id) DO NOTHING;

UPDATE ref_macro_series SET is_active = FALSE WHERE series_id = 'FRED:ADSINDEX';

-- 3. US LEI — switch to OECD CLI for the US (canonical free public leading indicator).
--    First ensure the underlying FRED native_id is whitelisted, then re-point.
INSERT INTO ref_fred_series (series_id, name, category, frequency, units, seasonal_adj, is_active)
VALUES ('USALOLITOAASTSAM',
        'OECD Composite Leading Indicator for the United States (amplitude-adjusted)',
        'nowcast', 'M', 'Index 100=long-run', TRUE, TRUE)
ON CONFLICT (series_id) DO NOTHING;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    ('FRED:USA_CLI', 'fred', 'USALOLITOAASTSAM',
     'OECD Composite Leading Indicator (US, amplitude-adjusted)',
     'nowcast', 'US', 'M', 'Index 100=long-run', TRUE, 2, 'us_lei')
ON CONFLICT (series_id) DO NOTHING;

UPDATE ref_macro_series SET is_active = FALSE WHERE series_id = 'FRED:USSLIND';

-- 4. Discontinued NYFED Staff Nowcast — deactivate.
UPDATE ref_macro_series SET is_active = FALSE WHERE series_id = 'NYFED:NOWCAST';

-- 5. ECB nowcast (not actually exposed by ECB SDMX) — deactivate.
UPDATE ref_macro_series SET is_active = FALSE WHERE series_id = 'ECB:NOWCAST_GDP';
