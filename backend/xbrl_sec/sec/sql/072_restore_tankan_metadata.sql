-- Restore Tankan metadata rows in sec.ref_macro_series.
--
-- Migration 040 originally seeded BOJ:TANKAN_LMFG and BOJ:TANKAN_LNMFG with
-- direct BOJ stat-search native_ids, but a subsequent BOJ refactor (which
-- re-pointed every other BOJ series at FRED mirrors) dropped these two
-- without a replacement — leaving the JP_TANKAN_FACTOR PCA compute starved.
--
-- Strategy: restore the original native_ids alongside FRED-mirror fallbacks.
-- The boj_ingest router prefers a `fred:` prefix when present and falls back
-- to direct stat-search scraping otherwise.
--   * BOJ:TANKAN_LMFG  ← OECD's Japan Business Confidence Indicator via FRED
--                       (JPNBSCICP02STSAQ — closest public Tankan proxy).
--   * BOJ:TANKAN_LNMFG ← restored with the original BOJ direct code; will
--                       remain empty until the BOJ scraper is hardened, but
--                       the metadata row is required so the PCA can register
--                       the slot.
--
-- Idempotent — safe to re-run.

SET search_path TO sec, public;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    ('BOJ:TANKAN_LMFG',  'boj', 'fred:JPNBSCICP02STSAQ',  'Tankan-style Business Confidence (large mfg proxy)',     'sentiment', 'JP', 'Q', 'Index 100=long-run',     FALSE, 1, 'jp_tankan_lmfg'),
    ('BOJ:TANKAN_LNMFG', 'boj', 'CO''CO''CO01''COBQB01',  'Tankan Large Non-Mfg Business Conditions DI',            'sentiment', 'JP', 'Q', 'DI Points',              FALSE, 2, 'jp_tankan_lnmfg')
ON CONFLICT (series_id) DO NOTHING;
