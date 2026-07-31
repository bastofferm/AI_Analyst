-- Sector-default WACCs used by the INTL committee path when Fama-French factor
-- regressions are unavailable (INTL companies have no fact_factor_loadings row).
--
-- Seeded values are rough industry priors, not calibrated. The INTL committee
-- surface explicitly cites the uncertainty band around these defaults so the
-- memo never presents them as precise.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_wacc_sector_default (
    sector_scope TEXT PRIMARY KEY,
    wacc_pct     NUMERIC NOT NULL,
    note         TEXT
);

INSERT INTO ref_wacc_sector_default (sector_scope, wacc_pct, note) VALUES
    ('corp',                          9.0,  'General corporate default; ~equity-heavy.'),
    ('bank_financial',                10.0, 'Higher equity cost of capital, regulatory risk.'),
    ('non_bank_financial',            9.5,  'Diversified financial services default.'),
    ('insurance',                     9.0,  'Insurers: moderate leverage, stable float.'),
    ('reit',                          8.0,  'REITs: lower WACC given interest-sensitive mix.'),
    ('asset_manager_other_financial', 9.5,  'Asset managers and other non-bank financials.')
ON CONFLICT (sector_scope) DO UPDATE
   SET wacc_pct = EXCLUDED.wacc_pct,
       note     = EXCLUDED.note;

COMMENT ON TABLE ref_wacc_sector_default IS
    'Sector-default WACCs used when factor-regression WACC is not available (e.g. INTL companies without Fama-French factor loadings).';
