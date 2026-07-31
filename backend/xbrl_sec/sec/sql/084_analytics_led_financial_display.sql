-- Analytics-led financial display layer.
--
-- This migration keeps the canonical standardized line item registry intact
-- and adds presentation metadata that ranks analytics, high-level line items,
-- supplemental rows, and audit-only rows for ticker-level research pages.

SET search_path TO sec, public;

ALTER TABLE ref_std_statement_display_profile
    ADD COLUMN IF NOT EXISTS display_section TEXT,
    ADD COLUMN IF NOT EXISTS priority_rank INTEGER,
    ADD COLUMN IF NOT EXISTS default_visibility TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS max_default_rows INTEGER;

ALTER TABLE ref_std_statement_display_profile
    DROP CONSTRAINT IF EXISTS ref_std_statement_display_profile_visibility_chk;

ALTER TABLE ref_std_statement_display_profile
    ADD CONSTRAINT ref_std_statement_display_profile_visibility_chk
    CHECK (default_visibility IN ('default', 'supplemental', 'audit_only', 'hidden'));

COMMENT ON COLUMN ref_std_statement_display_profile.display_section IS
    'Investor-facing display section used by the analytics-led view.';
COMMENT ON COLUMN ref_std_statement_display_profile.priority_rank IS
    'Lower values appear earlier inside a display section.';
COMMENT ON COLUMN ref_std_statement_display_profile.default_visibility IS
    'default rows appear in the compact display; supplemental/audit_only rows require expansion or drilldown.';
COMMENT ON COLUMN ref_std_statement_display_profile.max_default_rows IS
    'Optional row budget for the section in compact display.';

CREATE TABLE IF NOT EXISTS ref_financial_display_profile (
    profile_id          BIGSERIAL PRIMARY KEY,
    accounting_standard TEXT NOT NULL
        CHECK (accounting_standard IN ('US_GAAP', 'JP_GAAP', 'IFRS')),
    sector_scope        TEXT NOT NULL,
    source_type         TEXT NOT NULL
        CHECK (source_type IN ('metric', 'line_item', 'derived')),
    source_id           TEXT NOT NULL,
    display_section     TEXT NOT NULL,
    display_role        TEXT NOT NULL DEFAULT 'metric',
    priority_rank       INTEGER NOT NULL DEFAULT 9999,
    default_visibility  TEXT NOT NULL DEFAULT 'default'
        CHECK (default_visibility IN ('default', 'supplemental', 'audit_only', 'hidden')),
    max_default_rows    INTEGER,
    label_override      TEXT,
    unit_type_override  TEXT,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (accounting_standard, sector_scope, source_type, source_id, display_section)
);

COMMENT ON TABLE ref_financial_display_profile IS
    'Integrated ticker financial display profile. Ranks analytics/metrics above high-level standardized line items and keeps raw accounting rows out of the default view.';

CREATE INDEX IF NOT EXISTS idx_rf_display_profile_scope
    ON ref_financial_display_profile (accounting_standard, sector_scope, display_section, priority_rank);

WITH seeds(accounting_standard, sector_scope, source_type, source_id, display_section, display_role, priority_rank, default_visibility, max_default_rows, label_override, unit_type_override, note) AS (
    VALUES
    -- Corporate / universal profile.
    ('US_GAAP','corp','metric','revenue_growth_year_over_year','key_metrics','metric',100,'default',10,NULL,'DEC','Analytics first: growth rate before raw revenue.'),
    ('US_GAAP','corp','metric','revenue_compound_annual_growth_rate_5_year','key_metrics','metric',110,'default',10,NULL,'DEC','Five-year top-line compounding.'),
    ('US_GAAP','corp','metric','gross_margin','key_metrics','metric',120,'default',10,NULL,'DEC','Profitability metric.'),
    ('US_GAAP','corp','metric','operating_margin','key_metrics','metric',130,'default',10,NULL,'DEC','Operating profitability metric.'),
    ('US_GAAP','corp','metric','net_profit_margin','key_metrics','metric',140,'default',10,NULL,'DEC','Net profitability metric.'),
    ('US_GAAP','corp','metric','return_on_equity','key_metrics','metric',150,'default',10,NULL,'DEC','Capital return metric.'),
    ('US_GAAP','corp','metric','return_on_assets','key_metrics','metric',160,'default',10,NULL,'DEC','Asset return metric.'),
    ('US_GAAP','corp','metric','enterprise_value_to_earnings_before_interest_taxes_depreciation_amortization','key_metrics','metric',170,'default',10,'EV / EBITDA','RATIO','Valuation multiple when available.'),
    ('US_GAAP','corp','metric','free_cash_flow_yield','cash_generation','metric',300,'default',10,NULL,'DEC','Cash-generation valuation metric.'),
    ('US_GAAP','corp','metric','cash_flow_from_operations_yield','cash_generation','metric',310,'default',10,NULL,'DEC','Operating cash flow yield.'),
    ('US_GAAP','corp','metric','net_debt_to_earnings_before_interest_taxes_depreciation_amortization','balance_sheet_strength','metric',400,'default',12,NULL,'RATIO','Leverage metric.'),
    ('US_GAAP','corp','metric','total_financial_debt_to_equity','balance_sheet_strength','metric',410,'default',12,NULL,'RATIO','Capital structure metric.'),
    ('US_GAAP','corp','metric','cash_ratio','balance_sheet_strength','metric',420,'default',12,NULL,'DEC','Liquidity metric.'),
    ('US_GAAP','corp','line_item','revenue','statement_summary','high_level_line_item',1000,'default',14,NULL,'CCY','Top-line standardized line item.'),
    ('US_GAAP','corp','line_item','gross_profit','statement_summary','high_level_line_item',1010,'default',14,NULL,'CCY','High-level standardized line item.'),
    ('US_GAAP','corp','line_item','earnings_before_interest_taxes','statement_summary','high_level_line_item',1020,'default',14,'EBIT / Operating Income','CCY','Operating income proxy.'),
    ('US_GAAP','corp','line_item','net_income','statement_summary','high_level_line_item',1030,'default',14,NULL,'CCY','Bottom-line standardized line item.'),
    ('US_GAAP','corp','line_item','cash_flow_from_operations','cash_generation','high_level_line_item',1100,'default',10,NULL,'CCY','Operating cash flow.'),
    ('US_GAAP','corp','line_item','capital_expenditures','cash_generation','supporting_line_item',1110,'default',10,NULL,'CCY','Reinvestment support row.'),
    ('US_GAAP','corp','derived','free_cash_flow','cash_generation','derived_line_item',1120,'default',10,NULL,'CCY','Derived when not filed: CFO plus capex.'),
    ('US_GAAP','corp','line_item','total_assets','balance_sheet_strength','high_level_line_item',1210,'default',12,NULL,'CCY','Asset scale.'),
    ('US_GAAP','corp','line_item','total_liabilities','balance_sheet_strength','high_level_line_item',1220,'default',12,NULL,'CCY','Liability scale.'),
    ('US_GAAP','corp','line_item','total_equity','balance_sheet_strength','high_level_line_item',1230,'default',12,NULL,'CCY','Equity capital.'),
    ('US_GAAP','corp','derived','total_financial_debt','capital_structure','derived_line_item',1300,'default',10,NULL,'CCY','Debt subtotal from standardized debt rows when needed.'),
    ('US_GAAP','corp','derived','net_debt','capital_structure','derived_line_item',1310,'default',10,NULL,'CCY','Debt less cash when reliable.'),

    -- Bank financials.
    ('US_GAAP','bank_financial','metric','return_on_equity','key_metrics','metric',100,'default',10,NULL,'DEC','Bank profitability metric.'),
    ('US_GAAP','bank_financial','metric','return_on_assets','key_metrics','metric',110,'default',10,NULL,'DEC','Bank asset return metric.'),
    ('US_GAAP','bank_financial','metric','net_interest_margin','key_metrics','metric',120,'default',10,NULL,'DEC','Bank spread metric.'),
    ('US_GAAP','bank_financial','metric','loan_to_deposit_ratio','key_metrics','metric',130,'default',10,NULL,'DEC','Bank liquidity/funding metric.'),
    ('US_GAAP','bank_financial','metric','nonperforming_loan_ratio','key_metrics','metric',140,'default',10,NULL,'DEC','Bank credit quality metric.'),
    ('US_GAAP','bank_financial','metric','common_equity_tier1_ratio_metric','key_metrics','metric',150,'default',10,'CET1 Ratio','DEC','Bank capital metric.'),
    ('US_GAAP','bank_financial','line_item','total_net_revenue_bank','statement_summary','high_level_line_item',1000,'default',14,'Total Net Revenue','CCY','Bank revenue subtotal.'),
    ('US_GAAP','bank_financial','line_item','pre_provision_net_revenue','statement_summary','high_level_line_item',1020,'default',14,NULL,'CCY','Bank pre-provision profitability.'),
    ('US_GAAP','bank_financial','line_item','provision_for_loan_losses','statement_summary','supporting_line_item',1030,'default',14,NULL,'CCY','Credit cost support row.'),
    ('US_GAAP','bank_financial','line_item','total_loans_net','balance_sheet_strength','high_level_line_item',1100,'default',12,NULL,'CCY','Loan book.'),
    ('US_GAAP','bank_financial','line_item','total_deposits','balance_sheet_strength','high_level_line_item',1110,'default',12,NULL,'CCY','Deposit base.'),

    -- Non-bank financial subprofiles.
    ('US_GAAP','asset_manager_other_financial','line_item','assets_under_management','key_metrics','operating_metric',100,'default',10,NULL,'CCY','Asset manager operating scale.'),
    ('US_GAAP','asset_manager_other_financial','line_item','fee_earning_assets_under_management','key_metrics','operating_metric',110,'default',10,NULL,'CCY','Fee-bearing asset base.'),
    ('US_GAAP','asset_manager_other_financial','line_item','management_fee_revenue','statement_summary','high_level_line_item',1000,'default',14,NULL,'CCY','Fee revenue.'),
    ('US_GAAP','insurance','metric','premium_growth_rate','key_metrics','metric',100,'default',10,NULL,'DEC','Insurance growth metric.'),
    ('US_GAAP','insurance','metric','combined_ratio','key_metrics','metric',110,'default',10,NULL,'DEC','Insurance underwriting efficiency.'),
    ('US_GAAP','insurance','line_item','net_premiums_earned','statement_summary','high_level_line_item',1000,'default',14,NULL,'CCY','Insurance earned premium base.'),
    ('US_GAAP','reit','metric','funds_from_operations_to_debt','key_metrics','metric',100,'default',10,NULL,'RATIO','REIT leverage/cash flow metric.'),
    ('US_GAAP','reit','metric','net_debt_to_earnings_before_interest_taxes_depreciation_amortization_real_estate','key_metrics','metric',110,'default',10,'Net Debt / EBITDA','RATIO','Real-estate leverage metric.'),
    ('US_GAAP','reit','line_item','rental_revenue','statement_summary','high_level_line_item',1000,'default',14,NULL,'CCY','Real-estate revenue base.')
)
INSERT INTO ref_financial_display_profile
    (accounting_standard, sector_scope, source_type, source_id, display_section,
     display_role, priority_rank, default_visibility, max_default_rows,
     label_override, unit_type_override, note)
SELECT accounting_standard, sector_scope, source_type, source_id, display_section,
       display_role, priority_rank, default_visibility, max_default_rows,
       label_override, unit_type_override, note
FROM seeds
ON CONFLICT (accounting_standard, sector_scope, source_type, source_id, display_section)
DO UPDATE SET
    display_role = EXCLUDED.display_role,
    priority_rank = EXCLUDED.priority_rank,
    default_visibility = EXCLUDED.default_visibility,
    max_default_rows = EXCLUDED.max_default_rows,
    label_override = EXCLUDED.label_override,
    unit_type_override = EXCLUDED.unit_type_override,
    note = EXCLUDED.note,
    updated_at = now();

INSERT INTO ref_financial_display_profile
    (accounting_standard, sector_scope, source_type, source_id, display_section,
     display_role, priority_rank, default_visibility, max_default_rows,
     label_override, unit_type_override, note)
SELECT 'JP_GAAP', sector_scope, source_type, source_id, display_section,
       display_role, priority_rank, default_visibility, max_default_rows,
       label_override, unit_type_override,
       COALESCE(note, '') || ' JP profile seed; API still validates available facts per ticker.'
FROM ref_financial_display_profile
WHERE accounting_standard = 'US_GAAP'
ON CONFLICT (accounting_standard, sector_scope, source_type, source_id, display_section)
DO UPDATE SET
    display_role = EXCLUDED.display_role,
    priority_rank = EXCLUDED.priority_rank,
    default_visibility = EXCLUDED.default_visibility,
    max_default_rows = EXCLUDED.max_default_rows,
    label_override = EXCLUDED.label_override,
    unit_type_override = EXCLUDED.unit_type_override,
    note = EXCLUDED.note,
    updated_at = now();

UPDATE ref_std_statement_display_profile
   SET display_section = CASE
           WHEN statement_type = 'income_statement' THEN 'statement_summary'
           WHEN statement_type = 'cash_flow_statement' THEN 'cash_generation'
           WHEN statement_type = 'balance_sheet' THEN 'balance_sheet_strength'
           ELSE 'statement_summary'
       END,
       priority_rank = COALESCE(display_order, 9999),
       default_visibility = CASE
           WHEN display_policy = 'MAIN' THEN 'default'
           WHEN display_policy = 'SUPPLEMENTAL' THEN 'supplemental'
           WHEN display_policy = 'DRILLDOWN_ONLY' THEN 'audit_only'
           ELSE 'hidden'
       END,
       max_default_rows = CASE
           WHEN statement_type = 'income_statement' THEN 14
           WHEN statement_type = 'cash_flow_statement' THEN 10
           WHEN statement_type = 'balance_sheet' THEN 12
           ELSE 12
       END,
       updated_at = now()
 WHERE priority_rank IS NULL
    OR display_section IS NULL;
