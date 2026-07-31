-- Slim the display profiles: statements show statement lines; the metrics
-- complex owns analytics; KPIs live in the KPI layer.
--
-- Three cuts:
--   1. All HIDE rows. Once a profile exists, only profile rows with values
--      render. Cross-jurisdiction items (e.g. JP bank concepts in a US_GAAP
--      profile) can never have values for entities under that profile, so
--      the suppressions are dead weight.
--   2. Metrics-layer duplicates: growth rates, return ratios, underwriting
--      ratios, TBVPS, charge-off KPIs. These are computed downstream by the
--      metrics complex; the statement display should not replicate them.
--      Deleting the profile rows also disables the corresponding display
--      bridge computations in assembly.py (bridges skip targets without a
--      profile row).
--   3. Operating KPIs (AUM, net flows, dry powder) embedded in income
--      statement profiles. They are operating_kpi dictionary items, not
--      statement lines.
--
-- Kept on purpose: EPS basic/diluted, shares outstanding, dividends per
-- share (traditional bottom-of-IS rows) and all SUPPLEMENTAL drill-down
-- detail (renders only when filled).

SET search_path TO sec, public;

-- 1. Dead HIDE rows.
DELETE FROM ref_std_statement_display_profile
WHERE display_policy = 'HIDE';

-- 2. Metrics-layer duplicates.
DELETE FROM ref_std_statement_display_profile
WHERE line_item_id LIKE '%growth_year_over_year'
   OR line_item_id IN (
       'return_on_average_assets',
       'return_on_average_equity_bank',
       'tangible_book_value_per_share',
       'nonperforming_loan_ratio',
       'net_charge_offs',
       'combined_ratio',
       'loss_ratio',
       'expense_ratio_insurance_underwriting'
   );

-- 3. Operating KPIs out of statement profiles.
DELETE FROM ref_std_statement_display_profile
WHERE line_item_id IN (
    'assets_under_management',
    'fee_earning_assets_under_management',
    'net_flows',
    'dry_powder'
);
