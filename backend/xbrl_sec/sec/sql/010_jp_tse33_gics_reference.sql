-- Controlled JPX/TSE 33-sector to GICS mapping for JP master enrichment.
-- dim_company_jp keeps one canonical set of GICS columns; TSE33 is only an input.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_jp_tse33_gics (
    tse33_code INTEGER PRIMARY KEY,
    tse33_name_ja TEXT,
    tse33_name_en TEXT,
    gics_sector_code TEXT NOT NULL,
    gics_sector_name TEXT NOT NULL,
    gics_industry_group_code TEXT NOT NULL,
    gics_industry_group_name TEXT NOT NULL,
    gics_industry_code TEXT,
    gics_industry_name TEXT,
    gics_sub_industry_code TEXT,
    gics_sub_industry_name TEXT,
    source TEXT NOT NULL DEFAULT 'JPX listed company file + internal TSE33-GICS policy mapping',
    source_url TEXT NOT NULL DEFAULT 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls',
    effective_from DATE,
    effective_to DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ref_jp_tse33_gics (
    tse33_code, tse33_name_en,
    gics_sector_code, gics_sector_name,
    gics_industry_group_code, gics_industry_group_name,
    gics_industry_code, gics_industry_name,
    gics_sub_industry_code, gics_sub_industry_name
) VALUES
    (50, 'Fishery, Agriculture & Forestry', '30', 'Consumer Staples', '3020', 'Food, Beverage & Tobacco', '302020', 'Food Products', '30202010', 'Packaged Foods & Meats'),
    (1050, 'Mining', '15', 'Materials', '1510', 'Materials', '151040', 'Metals & Mining', '15104020', 'Diversified Metals & Mining'),
    (1400, 'Construction', '20', 'Industrials', '2010', 'Capital Goods', '201030', 'Construction & Engineering', '20103010', 'Construction & Engineering'),
    (2050, 'Construction', '20', 'Industrials', '2010', 'Capital Goods', '201030', 'Construction & Engineering', '20103010', 'Construction & Engineering'),
    (3050, 'Foods', '30', 'Consumer Staples', '3020', 'Food, Beverage & Tobacco', '302020', 'Food Products', '30202010', 'Packaged Foods & Meats'),
    (3100, 'Textiles & Apparels', '25', 'Consumer Discretionary', '2520', 'Consumer Durables & Apparel', '252030', 'Textiles, Apparel & Luxury Goods', '25203010', 'Apparel, Accessories & Luxury Goods'),
    (3150, 'Pulp & Paper', '15', 'Materials', '1510', 'Materials', '151050', 'Paper & Forest Products', '15105020', 'Paper Products'),
    (3200, 'Chemicals', '15', 'Materials', '1510', 'Materials', '151010', 'Chemicals', '15101050', 'Diversified Chemicals'),
    (3250, 'Pharmaceutical', '35', 'Health Care', '3520', 'Pharmaceuticals, Biotechnology & Life Sciences', '352020', 'Pharmaceuticals', '35202010', 'Pharmaceuticals'),
    (3300, 'Oil & Coal Products', '10', 'Energy', '1010', 'Energy', '101020', 'Oil, Gas & Consumable Fuels', '10102050', 'Oil & Gas Refining, Marketing & Transportation'),
    (3350, 'Rubber Products', '25', 'Consumer Discretionary', '2510', 'Automobiles & Components', '251010', 'Auto Components', '25101010', 'Auto Parts & Equipment'),
    (3400, 'Glass & Ceramics Products', '15', 'Materials', '1510', 'Materials', '151020', 'Construction Materials', '15102010', 'Construction Materials'),
    (3450, 'Iron & Steel', '15', 'Materials', '1510', 'Materials', '151040', 'Metals & Mining', '15104050', 'Steel'),
    (3500, 'Nonferrous Metals', '15', 'Materials', '1510', 'Materials', '151040', 'Metals & Mining', '15104020', 'Diversified Metals & Mining'),
    (3550, 'Metal Products', '20', 'Industrials', '2010', 'Capital Goods', '201060', 'Machinery', '20106020', 'Industrial Machinery'),
    (3600, 'Machinery', '20', 'Industrials', '2010', 'Capital Goods', '201060', 'Machinery', '20106020', 'Industrial Machinery'),
    (3650, 'Electric Appliances', '45', 'Information Technology', '4520', 'Technology Hardware & Equipment', '452010', 'Electronic Equipment, Instruments & Components', '45201020', 'Electronic Components'),
    (3700, 'Transportation Equipment', '25', 'Consumer Discretionary', '2510', 'Automobiles & Components', '251020', 'Automobiles', '25102010', 'Automobile Manufacturers'),
    (3750, 'Precision Instruments', '45', 'Information Technology', '4520', 'Technology Hardware & Equipment', '452010', 'Electronic Equipment, Instruments & Components', '45201010', 'Electronic Equipment & Instruments'),
    (3800, 'Other Products', '25', 'Consumer Discretionary', '2520', 'Consumer Durables & Apparel', '252010', 'Leisure Products', '25201010', 'Leisure Products'),
    (4050, 'Electric Power & Gas', '55', 'Utilities', '5510', 'Utilities', '551050', 'Multi-Utilities', '55105010', 'Multi-Utilities'),
    (5050, 'Land Transportation', '20', 'Industrials', '2030', 'Transportation', '203040', 'Ground Transportation', '20304010', 'Railroads'),
    (5100, 'Marine Transportation', '20', 'Industrials', '2030', 'Transportation', '203030', 'Marine Transportation', '20303010', 'Marine Transportation'),
    (5150, 'Air Transportation', '20', 'Industrials', '2030', 'Transportation', '203020', 'Airlines', '20302010', 'Airlines'),
    (5200, 'Warehousing & Harbor Transportation Services', '20', 'Industrials', '2030', 'Transportation', '203010', 'Air Freight & Logistics', '20301010', 'Air Freight & Logistics'),
    (5250, 'Information & Communication', '50', 'Communication Services', '5010', 'Telecommunication Services', '501020', 'Diversified Telecommunication Services', '50102010', 'Integrated Telecommunication Services'),
    (6050, 'Wholesale Trade', '20', 'Industrials', '2010', 'Capital Goods', '201070', 'Trading Companies & Distributors', '20107010', 'Trading Companies & Distributors'),
    (6100, 'Retail Trade', '25', 'Consumer Discretionary', '2550', 'Consumer Discretionary Distribution & Retail', '255040', 'Specialty Retail', '25504040', 'Specialty Stores'),
    (7050, 'Banks', '40', 'Financials', '4010', 'Banks', '401010', 'Banks', '40101010', 'Diversified Banks'),
    (7100, 'Securities & Commodity Futures', '40', 'Financials', '4020', 'Financial Services', '402010', 'Capital Markets', '40201020', 'Investment Banking & Brokerage'),
    (7150, 'Insurance', '40', 'Financials', '4030', 'Insurance', '403030', 'Multi-line Insurance', '40303010', 'Multi-line Insurance'),
    (7200, 'Other Financing Business', '40', 'Financials', '4020', 'Financial Services', '402020', 'Consumer Finance', '40202010', 'Consumer Finance'),
    (8050, 'Real Estate', '60', 'Real Estate', '6020', 'Real Estate Management & Development', '602020', 'Real Estate Management & Development', '60202020', 'Real Estate Development'),
    (9050, 'Services', '20', 'Industrials', '2020', 'Commercial & Professional Services', '202020', 'Professional Services', '20202020', 'Research & Consulting Services')
ON CONFLICT (tse33_code) DO UPDATE SET
    tse33_name_en = EXCLUDED.tse33_name_en,
    gics_sector_code = EXCLUDED.gics_sector_code,
    gics_sector_name = EXCLUDED.gics_sector_name,
    gics_industry_group_code = EXCLUDED.gics_industry_group_code,
    gics_industry_group_name = EXCLUDED.gics_industry_group_name,
    gics_industry_code = EXCLUDED.gics_industry_code,
    gics_industry_name = EXCLUDED.gics_industry_name,
    gics_sub_industry_code = EXCLUDED.gics_sub_industry_code,
    gics_sub_industry_name = EXCLUDED.gics_sub_industry_name,
    is_active = TRUE,
    updated_at = now();
