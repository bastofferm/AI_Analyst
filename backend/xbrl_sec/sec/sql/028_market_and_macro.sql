-- Market price extensions + FRED macro schema.
--
-- Adds adj_close/volume to price tables, creates ref_fred_series and
-- fact_macro_fred, and seeds 29 key macro series.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Extend existing price tables
-- ---------------------------------------------------------------------------

ALTER TABLE fact_prices_us
    ADD COLUMN IF NOT EXISTS adj_close DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS volume    BIGINT;

ALTER TABLE fact_prices_jp
    ADD COLUMN IF NOT EXISTS adj_close DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS volume    BIGINT;

-- ---------------------------------------------------------------------------
-- FRED series registry
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_fred_series (
    series_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    frequency    TEXT,
    units        TEXT,
    seasonal_adj BOOLEAN,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_fred_series IS
    'Registry of FRED macro series fetched and stored in fact_macro_fred.';

-- ---------------------------------------------------------------------------
-- FRED observations
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('sec.fact_macro') IS NULL
       AND to_regclass('sec.fact_macro_fred') IS NULL THEN
        CREATE TABLE fact_macro_fred (
            series_id  TEXT NOT NULL REFERENCES ref_fred_series (series_id),
            date       DATE NOT NULL,
            value      DOUBLE PRECISION,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (series_id, date)
        );

        COMMENT ON TABLE fact_macro_fred IS
            'Daily/monthly/quarterly observations for FRED macro series.';

        CREATE INDEX idx_macro_fred_date ON fact_macro_fred (date DESC, series_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Seed 29 key macro series
-- ---------------------------------------------------------------------------

INSERT INTO ref_fred_series (series_id, name, category, frequency, units, seasonal_adj) VALUES
    ('DFF',          'Federal Funds Rate (daily)',             'rates',        'D', 'Percent',            FALSE),
    ('FEDFUNDS',     'Federal Funds Effective Rate (monthly)', 'rates',        'M', 'Percent',            FALSE),
    ('DGS1MO',       '1-Month Treasury CMT',                  'rates',        'D', 'Percent',            FALSE),
    ('DGS3MO',       '3-Month Treasury CMT',                  'rates',        'D', 'Percent',            FALSE),
    ('DGS6MO',       '6-Month Treasury CMT',                  'rates',        'D', 'Percent',            FALSE),
    ('DGS1',         '1-Year Treasury CMT',                   'rates',        'D', 'Percent',            FALSE),
    ('DGS2',         '2-Year Treasury CMT',                   'rates',        'D', 'Percent',            FALSE),
    ('DGS5',         '5-Year Treasury CMT',                   'rates',        'D', 'Percent',            FALSE),
    ('DGS10',        '10-Year Treasury CMT',                  'rates',        'D', 'Percent',            FALSE),
    ('DGS30',        '30-Year Treasury CMT',                  'rates',        'D', 'Percent',            FALSE),
    ('T10YIE',       '10-Year Breakeven Inflation Rate',      'inflation',    'D', 'Percent',            FALSE),
    ('CPIAUCSL',     'CPI All Urban Consumers',               'inflation',    'M', 'Index 1982-84=100',  TRUE),
    ('CPILFESL',     'Core CPI (Less Food & Energy)',         'inflation',    'M', 'Index 1982-84=100',  TRUE),
    ('PCEPILFE',     'Core PCE Price Index',                  'inflation',    'M', 'Index 2012=100',     TRUE),
    ('GDP',          'Gross Domestic Product (Nominal)',       'growth',       'Q', 'Billions USD',       TRUE),
    ('GDPC1',        'Real GDP (Chained 2017 USD)',           'growth',       'Q', 'Billions USD',       TRUE),
    ('GDPCTPI',      'GDP Price Deflator',                    'inflation',    'Q', 'Index 2017=100',     TRUE),
    ('UNRATE',       'Unemployment Rate',                     'labor',        'M', 'Percent',            TRUE),
    ('PAYEMS',       'Total Nonfarm Payrolls',                'labor',        'M', 'Thousands',          TRUE),
    ('IC4WSA',       'Initial Claims 4-Week MA',              'labor',        'W', 'Number',             FALSE),
    ('BAMLH0A0HYM2', 'ICE BofA US High Yield OAS',           'credit',       'D', 'Percent',            FALSE),
    ('BAMLC0A0CM',   'ICE BofA US Corporate Bond OAS',       'credit',       'D', 'Percent',            FALSE),
    ('T3MFF',        '3-Month Treasury Less Fed Funds',       'credit',       'D', 'Percent',            FALSE),
    ('M2SL',         'M2 Money Supply',                       'money_supply', 'M', 'Billions USD',       TRUE),
    ('BOGMBASE',     'Monetary Base',                         'money_supply', 'W', 'Billions USD',       FALSE),
    ('CSUSHPISA',    'S&P/Case-Shiller Home Price Index',     'housing',      'M', 'Index Jan 2000=100', TRUE),
    ('HOUST',        'Housing Starts',                        'housing',      'M', 'Thousands units',    TRUE),
    ('VIXCLS',       'CBOE Volatility Index (VIX)',           'volatility',   'D', 'Index',              FALSE),
    ('DTWEXBGS',     'US Dollar Broad Index',                 'rates',        'D', 'Index Jan 2006=100', FALSE)
ON CONFLICT (series_id) DO NOTHING;
