-- Nowcast + liquidity series seeds for ref_macro_series.
--
-- Adds two new macro categories:
--   * 'nowcast'   — real-time business-cycle nowcasts (GDPNow, FRBNY, ADS, WEI, LEI,
--                   €-coin, ECB, Euroframe, Euro-STING, Tankan factor, JP FSI).
--   * 'liquidity' — central-bank balance sheets, RRP, TGA, bank reserves, derived
--                   Fed net liquidity and global liquidity aggregates.
--
-- Three new source registrations: cepr (Bank of Italy / CEPR), euroframe (Euroframe
-- network) and bde (Bank of Spain — Euro-STING).
--
-- Idempotent — safe to re-run.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Register new sources
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_source (source_id, name, jurisdiction, base_url, requires_api_key) VALUES
    ('cepr',      'Bank of Italy / CEPR (€-coin)', 'EZ', 'https://eurocoin.cepr.org',          FALSE),
    ('euroframe', 'Euroframe Indicator Network',   'EZ', 'https://www.euroframe.org',          FALSE),
    ('bde',       'Bank of Spain (Euro-STING)',    'EZ', 'https://www.bde.es',                 FALSE)
ON CONFLICT (source_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Nowcast series
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    -- United States
    ('FRED:GDPNOW',        'fred',      'GDPNOW',        'Atlanta Fed GDPNow (current quarter)',         'nowcast', 'US', 'D', 'Percent SAAR',         TRUE,  1, 'us_gdpnow'),
    ('NYFED:NOWCAST',      'nyfed',     'staff_nowcast', 'FRBNY Staff Nowcast (current quarter GDP)',    'nowcast', 'US', 'W', 'Percent SAAR',         TRUE,  1, 'us_nowcast_nyfed'),
    ('FRED:ADSINDEX',      'fred',      'ADSWEEKLY',     'Aruoba-Diebold-Scotti Business Conditions',    'nowcast', 'US', 'D', 'Index (std. units)',   FALSE, 2, 'us_ads'),
    ('NYFED:WEI',          'nyfed',     'wei',           'NY Fed Weekly Economic Index',                  'nowcast', 'US', 'W', 'Percent YoY-equiv',    TRUE,  1, 'us_wei'),
    ('FRED:USSLIND',       'fred',      'USSLIND',       'Conference Board Leading Economic Index proxy', 'nowcast', 'US', 'M', 'Index 2016=100',       TRUE,  2, 'us_lei'),
    -- Eurozone
    ('CEPR:ECOIN',         'cepr',      'eurocoin',      '€-coin Coincident Indicator (CEPR/Banca Italia)','nowcast','EZ', 'M', 'Percent (quarterly)',  TRUE,  1, 'ez_ecoin'),
    ('ECB:NOWCAST_GDP',    'ecb',       'nowcast_gdp',   'ECB Nowcasting Toolbox combined GDP nowcast',  'nowcast', 'EZ', 'M', 'Percent QoQ',          TRUE,  1, 'ez_ecb_nowcast'),
    ('EUROFRAME:EFI',      'euroframe', 'efi',           'Euroframe Indicator',                           'nowcast', 'EZ', 'Q', 'Index',                TRUE,  2, 'ez_euroframe'),
    ('BDE:EUROSTING',      'bde',       'eurosting',     'Bank of Spain Euro-STING nowcast',              'nowcast', 'EZ', 'Q', 'Percent QoQ',          TRUE,  2, 'ez_eurosting'),
    -- Japan — derived from BOJ Tankan + financial stress inputs
    ('COMPUTE:JP_TANKAN_FACTOR', 'compute', 'jp_tankan_factor', 'Tankan-based business-cycle factor (PCA)', 'nowcast', 'JP', 'Q', 'Std. units',     FALSE, 1, 'jp_tankan_factor'),
    ('COMPUTE:JP_FSI',           'compute', 'jp_fsi',           'Japan Financial Stress Index',              'nowcast', 'JP', 'D', 'Std. units',     FALSE, 2, 'jp_fsi')
ON CONFLICT (series_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Liquidity aggregates
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction, frequency, units, seasonal_adj, importance, story_tile_slot)
VALUES
    -- United States
    ('FRED:WALCL',         'fred', 'WALCL',         'Fed Total Assets (balance sheet)',           'liquidity', 'US', 'W', 'Millions USD',  FALSE, 1, 'us_fed_balance_sheet'),
    ('FRED:RRPONTSYD',     'fred', 'RRPONTSYD',     'Overnight Reverse Repo Operations',          'liquidity', 'US', 'D', 'Billions USD',  FALSE, 1, 'us_rrp'),
    ('FRED:WTREGEN',       'fred', 'WTREGEN',       'US Treasury General Account (TGA)',          'liquidity', 'US', 'W', 'Millions USD',  FALSE, 1, 'us_tga'),
    ('FRED:WRESBAL',       'fred', 'WRESBAL',       'Reserves of Depository Institutions',        'liquidity', 'US', 'W', 'Millions USD',  FALSE, 2, 'us_bank_reserves'),
    ('COMPUTE:NETLIQ',     'compute', 'us_net_liq', 'Fed Net Liquidity (WALCL − TGA − RRP)',      'liquidity', 'US', 'W', 'Millions USD',  FALSE, 1, 'us_net_liquidity'),
    -- Eurozone
    ('ECB:BS_TOTAL',       'ecb', 'ILM.W.U2.C.LT00.Z5.Z01', 'ECB Eurosystem Total Assets',         'liquidity', 'EZ', 'W', 'Millions EUR',  FALSE, 1, 'ez_ecb_balance_sheet'),
    -- Japan
    ('BOJ:BS_TOTAL',       'boj', 'MD02''MABS1AN01',         'BOJ Total Assets',                    'liquidity', 'JP', 'M', 'Billions JPY',  FALSE, 1, 'jp_boj_balance_sheet'),
    ('BOJ:CA_BAL',         'boj', 'MD''MD02''MABS1AN15',     'BOJ Current Account Balances',        'liquidity', 'JP', 'M', 'Billions JPY',  FALSE, 2, 'jp_boj_current_account'),
    -- Global aggregate
    ('COMPUTE:GLOBAL_LIQ', 'compute', 'global_liq', 'Global central-bank liquidity (USD-converted)', 'liquidity', 'XX', 'W', 'Trillions USD', FALSE, 1, 'global_liquidity')
ON CONFLICT (series_id) DO NOTHING;
