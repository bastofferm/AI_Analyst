-- Japan macro acquisition wave 1: BOJ policy, liquidity, credit, debt,
-- pipeline inflation, and Tankan condition indicators.

SET search_path TO sec, public;

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('BOJ:BASIC_LOAN_RATE', 'boj', 'api:IR01:MADR1Z@D',
     'BOJ Basic Discount Rate and Basic Loan Rate', 'rates', 'JP', 'D', 'Percent', FALSE, TRUE, 1, 'jp_policy_rate'),
    ('BOJ:BANK_LENDING_RATE', 'boj', 'api:IR04:DLLR2CIDBNL1',
     'Average Contract Interest Rate on New Loans and Discounts', 'rates', 'JP', 'M', 'Percent', FALSE, TRUE, 2, 'jp_bank_lending_rate'),

    ('BOJ:TREASURY_FUNDS_TOTAL', 'boj', 'api:MD06:MASDM2',
     'Treasury Funds and Others, Total', 'liquidity', 'JP', 'M', '100 million yen', FALSE, TRUE, 3, 'jp_treasury_funds_total'),
    ('BOJ:MARKET_OPS_JGB_OVER1Y', 'boj', 'api:MD06:MASDM253',
     'Treasury Funds and Others, Government Bonds over One Year', 'liquidity', 'JP', 'M', '100 million yen', FALSE, TRUE, 3, 'jp_jgb_market_ops'),
    ('BOJ:EXCESS_RESERVES_CITY', 'boj', 'api:MD08:MACAB1013',
     'City Banks Excess Reserves at BOJ', 'liquidity', 'JP', 'M', '100 million yen', FALSE, TRUE, 3, 'jp_excess_reserves_city_banks'),

    ('BOJ:CGPI_ALL', 'boj', 'api:PR01:PRCG20_2200000000',
     'Corporate Goods Price Index, All Commodities', 'inflation', 'JP', 'M', 'Index 2020=100', FALSE, TRUE, 3, NULL),
    ('BOJ:SPPI_ALL', 'boj', 'api:PR02:PRCS20_5200000000',
     'Services Producer Price Index, All Items', 'inflation', 'JP', 'M', 'Index 2020=100', FALSE, TRUE, 3, NULL),

    ('BOJ:NATIONAL_GOV_DEBT', 'boj', 'api:PF02:PFGD1',
     'National Government Debt, Total', 'debt', 'JP', 'M', '100 million yen', FALSE, TRUE, 1, 'jp_public_debt_total'),
    ('BOJ:BANK_LOANS_TOTAL', 'boj', 'api:LA01:DLLILKG90_DLLI5DS2T',
     'Loans and Bills Discounted, Total Outstanding', 'debt', 'JP', 'Q', '100 million yen', FALSE, TRUE, 2, 'jp_bank_loans_total'),
    ('BOJ:COMMITMENT_LINES', 'boj', 'api:LA04:DLCM01',
     'Commitment Lines Outstanding', 'debt', 'JP', 'M', '100 million yen', FALSE, TRUE, 2, 'jp_commitment_lines'),
    ('BOJ:LOAN_DEMAND_FIRMS', 'boj', 'api:LA05:DLLSDLPB',
     'Senior Loan Officer DI for Loan Demand, Firms', 'debt', 'JP', 'Q', 'DI Points', FALSE, TRUE, 2, 'jp_loan_demand_firms'),

    ('BOJ:TANKAN_PRODUCTION_CAPACITY', 'boj', 'api:CO:TK99F0000607GCQ01000',
     'Tankan Production Capacity DI, Large Enterprises All Industries', 'activity', 'JP', 'Q', 'DI Points', FALSE, TRUE, 3, 'jp_production_capacity_di'),
    ('BOJ:TANKAN_EMPLOYMENT_CONDITIONS', 'boj', 'api:CO:TK99F0000608GCQ01000',
     'Tankan Employment Conditions DI, Large Enterprises All Industries', 'labor', 'JP', 'Q', 'DI Points', FALSE, TRUE, 2, 'jp_labor_demand'),
    ('BOJ:TANKAN_FINANCIAL_POSITION', 'boj', 'api:CO:TK99F0000609GCQ01000',
     'Tankan Financial Position DI, Large Enterprises All Industries', 'debt', 'JP', 'Q', 'DI Points', FALSE, TRUE, 2, 'jp_financial_position_di'),
    ('BOJ:TANKAN_LENDING_ATTITUDE', 'boj', 'api:CO:TK99F0000612GCQ00000',
     'Tankan Lending Attitude DI, All Enterprises All Industries', 'debt', 'JP', 'Q', 'DI Points', FALSE, TRUE, 2, 'jp_lending_attitude_di')
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

INSERT INTO ref_macro_series
    (series_id, source_id, native_id, name, category, jurisdiction,
     frequency, units, seasonal_adj, is_active, importance, story_tile_slot)
VALUES
    ('COMPUTE:JP_CGPI_YOY', 'compute', 'jp_cgpi_yoy',
     'Corporate Goods Price Index YoY', 'inflation', 'JP', 'M', 'Percent', FALSE, TRUE, 2, 'jp_cgpi_yoy'),
    ('COMPUTE:JP_SPPI_YOY', 'compute', 'jp_sppi_yoy',
     'Services Producer Price Index YoY', 'inflation', 'JP', 'M', 'Percent', FALSE, TRUE, 2, 'jp_sppi_yoy'),
    ('COMPUTE:JP_BANK_LOANS_YOY', 'compute', 'jp_bank_loans_yoy',
     'Bank Loans Outstanding YoY', 'debt', 'JP', 'Q', 'Percent', FALSE, TRUE, 2, 'jp_bank_loans_yoy'),
    ('COMPUTE:JP_PUBLIC_DEBT_YOY', 'compute', 'jp_public_debt_yoy',
     'National Government Debt YoY', 'debt', 'JP', 'M', 'Percent', FALSE, TRUE, 3, 'jp_public_debt_yoy')
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
