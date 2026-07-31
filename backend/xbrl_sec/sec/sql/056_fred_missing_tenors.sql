-- 056_fred_missing_tenors.sql
--
-- Adds three missing US Treasury yield-curve tenors (3Y, 7Y, 20Y) plus
-- the 10Y TIPS real-yield series (DFII10). These complete the 11-tenor
-- yield curve and unblock the macro-signals real-yield tile.
--
-- Idempotent: ON CONFLICT DO NOTHING.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Legacy ref_fred_series (kept in sync with migration 028)
-- ---------------------------------------------------------------------------

INSERT INTO ref_fred_series (series_id, name, category, frequency, units, seasonal_adj)
VALUES
    ('DGS3',   '3-Year Treasury CMT',            'rates', 'D', 'Percent', FALSE),
    ('DGS7',   '7-Year Treasury CMT',            'rates', 'D', 'Percent', FALSE),
    ('DGS20',  '20-Year Treasury CMT',           'rates', 'D', 'Percent', FALSE),
    ('DFII10', '10-Year TIPS Constant Maturity', 'rates', 'D', 'Percent', FALSE)
ON CONFLICT (series_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Multisource ref_macro_series (the table the API reads from)
-- ---------------------------------------------------------------------------

INSERT INTO ref_macro_series
  (series_id, source_id, native_id, name, category, jurisdiction,
   frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
  ('FRED:DGS3',   'fred', 'DGS3',   '3-Year Treasury CMT',        'rates', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_3y_yield'),
  ('FRED:DGS7',   'fred', 'DGS7',   '7-Year Treasury CMT',        'rates', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_7y_yield'),
  ('FRED:DGS20',  'fred', 'DGS20',  '20-Year Treasury CMT',       'rates', 'US', 'D', 'Percent', FALSE, TRUE, 2, 'us_20y_yield'),
  ('FRED:DFII10', 'fred', 'DFII10', '10-Year TIPS Real Yield',    'rates', 'US', 'D', 'Percent', FALSE, TRUE, 1, 'us_real_yield_10y')
ON CONFLICT (series_id) DO NOTHING;
