-- 060_ecb_snb_seeds.sql
--
-- Seeds ECB (Eurozone) and SNB (Switzerland) macro series in ref_macro_series.
-- Uses RAW native source identifiers (NOT the 'fred:' prefix that
-- fred_proxy_ingest.py looks for) — these series are fetched by the new
-- native modules ecb_ingest.py and snb_ingest.py.
--
-- Idempotent: ON CONFLICT DO NOTHING.

SET search_path TO sec, public;

INSERT INTO ref_macro_series
  (series_id, source_id, native_id, name, category, jurisdiction,
   frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
  -- =====================================================================
  -- ECB (Eurozone) — native SDMX series keys for data-api.ecb.europa.eu
  -- =====================================================================
  ('ECB:MNA_REAL_GDP_YOY',  'ecb', 'MNA.Q.Y.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.GY',
   'EZ Real GDP YoY',                   'growth',    'EZ', 'Q', 'Percent', TRUE,  TRUE, 1, 'ez_real_gdp_yoy'),
  ('ECB:ICP_HICP_YOY',      'ecb', 'ICP.M.U2.N.000000.4.ANR',
   'EZ HICP YoY',                       'inflation', 'EZ', 'M', 'Percent', TRUE,  TRUE, 1, 'ez_hicp_yoy'),
  ('ECB:MRR',               'ecb', 'FM.B.U2.EUR.4F.KR.MRR_FR.LEV',
   'ECB Main Refinancing Rate',         'rates',     'EZ', 'D', 'Percent', FALSE, TRUE, 1, 'ez_policy_rate'),
  ('ECB:DFR',               'ecb', 'FM.B.U2.EUR.4F.KR.DFR.LEV',
   'ECB Deposit Facility Rate',         'rates',     'EZ', 'D', 'Percent', FALSE, TRUE, 2, 'ez_deposit_rate'),
  ('ECB:BUND_10Y',          'ecb', 'YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y',
   'EA 10Y AAA Government Bond Yield',  'rates',     'EZ', 'D', 'Percent', FALSE, TRUE, 1, 'ez_10y_yield'),
  ('ECB:UNEMPLOYMENT',      'ecb', 'LFSI.M.I9.S.UNEHRT.TOTAL0.15_74.T',
   'EZ Unemployment Rate',              'labor',     'EZ', 'M', 'Percent', TRUE,  TRUE, 2, 'ez_unemployment'),

  -- =====================================================================
  -- SNB (Switzerland) — cube IDs for data.snb.ch/api/cube
  -- =====================================================================
  ('SNB:GDP_REAL_YOY',      'snb', 'gdpqaag',
   'CH Real GDP YoY',                   'growth',    'CH', 'Q', 'Percent', TRUE,  TRUE, 1, 'ch_real_gdp_yoy'),
  ('SNB:CPI_YOY',           'snb', 'plkopr',
   'CH CPI YoY',                        'inflation', 'CH', 'M', 'Percent', TRUE,  TRUE, 1, 'ch_cpi_yoy'),
  ('SNB:POLICY_RATE',       'snb', 'snbintrt',
   'SNB Policy Rate',                   'rates',     'CH', 'D', 'Percent', FALSE, TRUE, 1, 'ch_policy_rate'),
  ('SNB:CONF_10Y',          'snb', 'rendoblim',
   'CH 10Y Confederation Bond Yield',   'rates',     'CH', 'D', 'Percent', FALSE, TRUE, 1, 'ch_10y_yield'),
  ('SNB:CHF_USD',           'snb', 'devkua',
   'CHF per USD (reference rate)',      'fx',        'CH', 'D', 'CHF',     FALSE, TRUE, 2, 'ch_chf_usd'),
  ('SNB:KOF_BAROMETER',     'snb', 'kofkbeko',
   'KOF Economic Barometer',            'sentiment', 'CH', 'M', 'Index',   TRUE,  TRUE, 2, 'ch_kof_barometer')
ON CONFLICT (series_id) DO NOTHING;
