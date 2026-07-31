-- Curate ref_std_statement_display_profile rows for the sector_scope/
-- statement_type combos that were missing. Today only
-- (US_GAAP, corp, income_statement) exists; everything else falls through
-- to the heuristic in _fetch_profile_rows which produces an unstructured
-- alphabetical-ish dump.
--
-- Adds 8 US_GAAP profiles:
--   - corp balance_sheet
--   - corp cash_flow_statement
--   - bank_financial income_statement
--   - bank_financial balance_sheet
--   - bank_financial cash_flow_statement
--   - insurance income_statement
--   - reit income_statement
--   - asset_manager_other_financial income_statement
--
-- JP_GAAP profiles and the remaining non-bank-financial BS/CF combos
-- follow this same pattern and can be added incrementally.

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- helper: idempotent insert
-- ---------------------------------------------------------------------------

DELETE FROM ref_std_statement_display_profile
WHERE accounting_standard = 'US_GAAP'
  AND ((sector_scope, statement_type) IN (
        ('corp', 'balance_sheet'),
        ('corp', 'cash_flow_statement'),
        ('bank_financial', 'income_statement'),
        ('bank_financial', 'balance_sheet'),
        ('bank_financial', 'cash_flow_statement'),
        ('insurance', 'income_statement'),
        ('reit', 'income_statement'),
        ('asset_manager_other_financial', 'income_statement')
       ));

-- ---------------------------------------------------------------------------
-- 1. US_GAAP corp balance_sheet
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Current assets
    ('US_GAAP','corp','balance_sheet','cash_and_cash_equivalents','WATERFALL','MAIN',1010,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','short_term_investments','WATERFALL','MAIN',1020,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','accounts_receivable_net','WATERFALL','MAIN',1030,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','accounts_receivable_gross','DISCLOSURE','SUPPLEMENTAL',1031,'accounts_receivable_net',2,NULL),
    ('US_GAAP','corp','balance_sheet','allowance_for_doubtful_accounts','DISCLOSURE','SUPPLEMENTAL',1032,'accounts_receivable_net',2,NULL),
    ('US_GAAP','corp','balance_sheet','inventory_total','WATERFALL','MAIN',1040,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','inventory_raw_materials','DISCLOSURE','SUPPLEMENTAL',1041,'inventory_total',2,NULL),
    ('US_GAAP','corp','balance_sheet','inventory_work_in_progress','DISCLOSURE','SUPPLEMENTAL',1042,'inventory_total',2,NULL),
    ('US_GAAP','corp','balance_sheet','inventory_finished_goods','DISCLOSURE','SUPPLEMENTAL',1043,'inventory_total',2,NULL),
    ('US_GAAP','corp','balance_sheet','prepaid_expenses','WATERFALL','MAIN',1050,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','other_current_assets','DISCLOSURE','MAIN',1060,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_current_assets','SUBTOTAL','MAIN',1090,NULL,1,NULL),
    -- Non-current assets
    ('US_GAAP','corp','balance_sheet','property_plant_equipment_net','WATERFALL','MAIN',1110,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','property_plant_equipment_gross','DISCLOSURE','SUPPLEMENTAL',1111,'property_plant_equipment_net',2,NULL),
    ('US_GAAP','corp','balance_sheet','accumulated_depreciation','DISCLOSURE','SUPPLEMENTAL',1112,'property_plant_equipment_net',2,NULL),
    ('US_GAAP','corp','balance_sheet','right_of_use_assets','WATERFALL','MAIN',1120,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','goodwill','WATERFALL','MAIN',1130,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','intangible_assets_net','WATERFALL','MAIN',1140,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','equity_method_investments','WATERFALL','MAIN',1150,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','deferred_tax_assets','WATERFALL','MAIN',1160,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','restricted_cash','WATERFALL','MAIN',1170,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','other_noncurrent_assets','DISCLOSURE','MAIN',1180,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_noncurrent_assets','SUBTOTAL','MAIN',1190,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_assets','SUBTOTAL','MAIN',1200,NULL,1,NULL),
    -- Current liabilities
    ('US_GAAP','corp','balance_sheet','accounts_payable','WATERFALL','MAIN',2010,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','accrued_liabilities','WATERFALL','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','deferred_revenue_current','WATERFALL','MAIN',2030,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','short_term_debt','WATERFALL','MAIN',2040,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','long_term_debt_current_portion','WATERFALL','MAIN',2050,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','other_current_liabilities','DISCLOSURE','MAIN',2060,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_current_liabilities','SUBTOTAL','MAIN',2090,NULL,1,NULL),
    -- Non-current liabilities
    ('US_GAAP','corp','balance_sheet','long_term_debt','WATERFALL','MAIN',2110,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','operating_lease_liability','WATERFALL','MAIN',2120,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','finance_lease_liability','WATERFALL','MAIN',2130,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','deferred_tax_liability','WATERFALL','MAIN',2140,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','pension_liabilities','WATERFALL','MAIN',2150,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','other_noncurrent_liabilities','DISCLOSURE','MAIN',2160,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_noncurrent_liabilities','SUBTOTAL','MAIN',2190,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_liabilities','SUBTOTAL','MAIN',2200,NULL,1,NULL),
    -- Equity
    ('US_GAAP','corp','balance_sheet','common_stock_par_value','WATERFALL','MAIN',3010,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','additional_paid_in_capital','WATERFALL','MAIN',3020,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','retained_earnings','WATERFALL','MAIN',3030,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','accumulated_other_comprehensive_income','WATERFALL','MAIN',3040,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','treasury_stock','WATERFALL','MAIN',3050,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','noncontrolling_interest_equity','WATERFALL','MAIN',3060,NULL,1,NULL),
    ('US_GAAP','corp','balance_sheet','total_equity','TOTAL','MAIN',3090,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 2. US_GAAP corp cash_flow_statement
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Operating
    ('US_GAAP','corp','cash_flow_statement','cash_flow_from_operations','SUBTOTAL','MAIN',1090,NULL,1,'Operating cash flow header'),
    ('US_GAAP','corp','cash_flow_statement','depreciation_and_amortization_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1010,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','stock_based_compensation_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1020,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','asset_impairment_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1030,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','deferred_tax_cashflow_impact','DISCLOSURE','SUPPLEMENTAL',1040,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','gain_loss_on_asset_sale_cashflow','DISCLOSURE','SUPPLEMENTAL',1050,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','change_in_accounts_receivable','DISCLOSURE','SUPPLEMENTAL',1060,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','change_in_inventory','DISCLOSURE','SUPPLEMENTAL',1070,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','change_in_accounts_payable','DISCLOSURE','SUPPLEMENTAL',1080,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','change_in_other_working_capital','DISCLOSURE','SUPPLEMENTAL',1085,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','corp','cash_flow_statement','other_operating_activities','DISCLOSURE','SUPPLEMENTAL',1088,'cash_flow_from_operations',2,NULL),
    -- Investing
    ('US_GAAP','corp','cash_flow_statement','capital_expenditures','WATERFALL','MAIN',2010,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','acquisitions','WATERFALL','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','divestitures','WATERFALL','MAIN',2030,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','purchase_of_investments','WATERFALL','MAIN',2040,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','sale_of_investments','WATERFALL','MAIN',2050,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','other_investing_activities','DISCLOSURE','MAIN',2060,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','cash_flow_from_investing','SUBTOTAL','MAIN',2090,NULL,1,NULL),
    -- Financing
    ('US_GAAP','corp','cash_flow_statement','debt_issuance_proceeds','WATERFALL','MAIN',3010,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','debt_repayment','WATERFALL','MAIN',3020,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','equity_issuance_proceeds','WATERFALL','MAIN',3030,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','share_repurchases','WATERFALL','MAIN',3040,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','dividends_paid','WATERFALL','MAIN',3050,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','other_financing_activities','DISCLOSURE','MAIN',3060,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','cash_flow_from_financing','SUBTOTAL','MAIN',3090,NULL,1,NULL),
    -- Reconciliation
    ('US_GAAP','corp','cash_flow_statement','fx_effect_on_cash','WATERFALL','MAIN',4010,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','net_change_in_cash','SUBTOTAL','MAIN',4020,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','beginning_cash_balance','WATERFALL','MAIN',4030,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','ending_cash_balance','TOTAL','MAIN',4040,NULL,1,NULL),
    -- Supplemental disclosures
    ('US_GAAP','corp','cash_flow_statement','interest_paid_cashflow','DISCLOSURE','SUPPLEMENTAL',5010,NULL,1,NULL),
    ('US_GAAP','corp','cash_flow_statement','taxes_paid_cashflow','DISCLOSURE','SUPPLEMENTAL',5020,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 3. US_GAAP bank_financial income_statement
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Net interest income block
    ('US_GAAP','bank_financial','income_statement','interest_income','DISCLOSURE','SUPPLEMENTAL',1010,'total_interest_income',2,NULL),
    ('US_GAAP','bank_financial','income_statement','total_interest_income','SUBTOTAL','MAIN',1090,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','interest_expense','DISCLOSURE','SUPPLEMENTAL',1110,'total_interest_expense',2,NULL),
    ('US_GAAP','bank_financial','income_statement','total_interest_expense','SUBTOTAL','MAIN',1190,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_interest_income','SUBTOTAL','MAIN',1200,NULL,1,'NII = total interest income - total interest expense'),
    -- Provisions
    ('US_GAAP','bank_financial','income_statement','provision_for_loan_losses','WATERFALL','MAIN',1300,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_interest_income_after_provision','SUBTOTAL','MAIN',1400,NULL,1,'NII after provisions'),
    -- Non-interest income
    ('US_GAAP','bank_financial','income_statement','fee_income','DISCLOSURE','SUPPLEMENTAL',1510,'non_interest_income',2,NULL),
    ('US_GAAP','bank_financial','income_statement','trading_income','DISCLOSURE','SUPPLEMENTAL',1520,'non_interest_income',2,NULL),
    ('US_GAAP','bank_financial','income_statement','non_interest_income','SUBTOTAL','MAIN',1590,NULL,1,NULL),
    -- Non-interest expense
    ('US_GAAP','bank_financial','income_statement','fdic_insurance_expense','DISCLOSURE','SUPPLEMENTAL',1610,'non_interest_expense',2,NULL),
    ('US_GAAP','bank_financial','income_statement','amortization_of_core_deposit_intangibles','DISCLOSURE','SUPPLEMENTAL',1620,'non_interest_expense',2,NULL),
    ('US_GAAP','bank_financial','income_statement','non_interest_expense','SUBTOTAL','MAIN',1690,NULL,1,NULL),
    -- Bridge
    ('US_GAAP','bank_financial','income_statement','pre_provision_net_revenue','CALCULATED','MAIN',1700,NULL,1,'PPNR = NII + non-interest income - non-interest expense'),
    -- To the bottom line
    ('US_GAAP','bank_financial','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    -- Per-share
    ('US_GAAP','bank_financial','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2010,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','shares_outstanding_basic','CALCULATED','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','shares_outstanding_diluted','CALCULATED','MAIN',2030,NULL,1,NULL),
    -- Bank KPIs
    ('US_GAAP','bank_financial','income_statement','return_on_average_assets','DISCLOSURE','MAIN',2100,NULL,1,'ROAA'),
    ('US_GAAP','bank_financial','income_statement','return_on_average_equity_bank','DISCLOSURE','MAIN',2110,NULL,1,'ROAE'),
    ('US_GAAP','bank_financial','income_statement','nonperforming_loan_ratio','DISCLOSURE','MAIN',2200,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_charge_offs','DISCLOSURE','MAIN',2210,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','tangible_book_value_per_share','DISCLOSURE','MAIN',2220,NULL,1,NULL),
    -- JP bank items hidden from US bank statements (they have their own JP_GAAP profile in a later migration)
    ('US_GAAP','bank_financial','income_statement','interest_on_loans_jp','HIDDEN','HIDE',9001,NULL,1,'JP-specific; rendered via JP_GAAP profile'),
    ('US_GAAP','bank_financial','income_statement','interest_dividends_securities_jp','HIDDEN','HIDE',9002,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','interest_call_loans_jp','HIDDEN','HIDE',9003,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','interest_due_from_banks_jp','HIDDEN','HIDE',9004,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_fund_management_jp','HIDDEN','HIDE',9005,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','fee_expense_jp','HIDDEN','HIDE',9006,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','non_personnel_expense_jp','HIDDEN','HIDE',9007,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','local_tax_bank_jp','HIDDEN','HIDE',9008,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','remittance_fees_jp','HIDDEN','HIDE',9009,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','bond_sale_gain_jp','HIDDEN','HIDE',9010,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','bond_sale_loss_jp','HIDDEN','HIDE',9011,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','fx_trading_gain_jp','HIDDEN','HIDE',9012,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','fx_trading_loss_jp','HIDDEN','HIDE',9013,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','trading_expense_jp','HIDDEN','HIDE',9014,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_business_income_misc_jp','HIDDEN','HIDE',9015,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_business_expense_misc_jp','HIDDEN','HIDE',9016,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_business_income_jp','HIDDEN','HIDE',9017,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_business_expense_jp','HIDDEN','HIDE',9018,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_ordinary_income_jp','HIDDEN','HIDE',9019,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_ordinary_expense_jp','HIDDEN','HIDE',9020,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','other_fee_income_jp','HIDDEN','HIDE',9021,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','total_net_revenue_bank','HIDDEN','HIDE',9022,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','ordinary_revenue_bank_japan_gaap','HIDDEN','HIDE',9023,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','ordinary_expenses_bank_jp','HIDDEN','HIDE',9024,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','net_business_profit_bank_japan_gaap','HIDDEN','HIDE',9025,NULL,1,NULL),
    ('US_GAAP','bank_financial','income_statement','gross_banking_profit','HIDDEN','HIDE',9026,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 4. US_GAAP bank_financial balance_sheet
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Liquid assets
    ('US_GAAP','bank_financial','balance_sheet','cash_and_cash_equivalents','WATERFALL','MAIN',1010,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','short_term_investments','DISCLOSURE','SUPPLEMENTAL',1020,'earning_assets',2,NULL),
    -- Earning assets
    ('US_GAAP','bank_financial','balance_sheet','total_loans_net','DISCLOSURE','SUPPLEMENTAL',1110,'earning_assets',2,NULL),
    ('US_GAAP','bank_financial','balance_sheet','allowance_for_loan_losses','DISCLOSURE','SUPPLEMENTAL',1120,'earning_assets',2,NULL),
    ('US_GAAP','bank_financial','balance_sheet','earning_assets','SUBTOTAL','MAIN',1190,NULL,1,'Loans net + investment securities + other earning assets'),
    -- Other assets
    ('US_GAAP','bank_financial','balance_sheet','goodwill','WATERFALL','MAIN',1210,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','intangible_assets_net','WATERFALL','MAIN',1220,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','property_plant_equipment_net','WATERFALL','MAIN',1230,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','deferred_tax_assets','WATERFALL','MAIN',1240,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','other_noncurrent_assets','DISCLOSURE','MAIN',1250,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','total_assets','SUBTOTAL','MAIN',1300,NULL,1,NULL),
    -- Liabilities
    ('US_GAAP','bank_financial','balance_sheet','total_deposits','SUBTOTAL','MAIN',2010,NULL,1,'Customer deposits'),
    ('US_GAAP','bank_financial','balance_sheet','short_term_debt','WATERFALL','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','long_term_debt','WATERFALL','MAIN',2030,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','deferred_tax_liability','WATERFALL','MAIN',2040,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','pension_liabilities','WATERFALL','MAIN',2050,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','other_noncurrent_liabilities','DISCLOSURE','MAIN',2060,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','total_liabilities','SUBTOTAL','MAIN',2090,NULL,1,NULL),
    -- Equity
    ('US_GAAP','bank_financial','balance_sheet','common_stock_par_value','WATERFALL','MAIN',3010,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','additional_paid_in_capital','WATERFALL','MAIN',3020,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','retained_earnings','WATERFALL','MAIN',3030,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','accumulated_other_comprehensive_income','WATERFALL','MAIN',3040,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','treasury_stock','WATERFALL','MAIN',3050,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','noncontrolling_interest_equity','WATERFALL','MAIN',3060,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','total_equity','TOTAL','MAIN',3090,NULL,1,NULL),
    -- Regulatory / asset-quality KPIs
    ('US_GAAP','bank_financial','balance_sheet','nonperforming_loans','DISCLOSURE','SUPPLEMENTAL',4010,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','risk_weighted_assets','DISCLOSURE','SUPPLEMENTAL',4020,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','common_equity_tier1_ratio','DISCLOSURE','SUPPLEMENTAL',4030,NULL,1,NULL),
    ('US_GAAP','bank_financial','balance_sheet','tier1_leverage_ratio','DISCLOSURE','SUPPLEMENTAL',4040,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 5. US_GAAP bank_financial cash_flow_statement
--    Banks use the universal CF structure; just curate the order.
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('US_GAAP','bank_financial','cash_flow_statement','cash_flow_from_operations','SUBTOTAL','MAIN',1090,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','depreciation_and_amortization_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1010,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','stock_based_compensation_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1020,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','asset_impairment_addback_cashflow','DISCLOSURE','SUPPLEMENTAL',1030,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','deferred_tax_cashflow_impact','DISCLOSURE','SUPPLEMENTAL',1040,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','other_operating_activities','DISCLOSURE','SUPPLEMENTAL',1085,'cash_flow_from_operations',2,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','capital_expenditures','WATERFALL','MAIN',2010,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','acquisitions','WATERFALL','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','divestitures','WATERFALL','MAIN',2030,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','purchase_of_investments','WATERFALL','MAIN',2040,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','sale_of_investments','WATERFALL','MAIN',2050,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','other_investing_activities','DISCLOSURE','MAIN',2060,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','cash_flow_from_investing','SUBTOTAL','MAIN',2090,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','debt_issuance_proceeds','WATERFALL','MAIN',3010,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','debt_repayment','WATERFALL','MAIN',3020,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','equity_issuance_proceeds','WATERFALL','MAIN',3030,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','share_repurchases','WATERFALL','MAIN',3040,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','dividends_paid','WATERFALL','MAIN',3050,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','other_financing_activities','DISCLOSURE','MAIN',3060,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','cash_flow_from_financing','SUBTOTAL','MAIN',3090,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','fx_effect_on_cash','WATERFALL','MAIN',4010,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','net_change_in_cash','SUBTOTAL','MAIN',4020,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','beginning_cash_balance','WATERFALL','MAIN',4030,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','ending_cash_balance','TOTAL','MAIN',4040,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','interest_paid_cashflow','DISCLOSURE','SUPPLEMENTAL',5010,NULL,1,NULL),
    ('US_GAAP','bank_financial','cash_flow_statement','taxes_paid_cashflow','DISCLOSURE','SUPPLEMENTAL',5020,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 6. US_GAAP insurance income_statement
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Premium revenue block
    ('US_GAAP','insurance','income_statement','gross_premiums_written','DISCLOSURE','SUPPLEMENTAL',1010,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','ceded_premiums_written','DISCLOSURE','SUPPLEMENTAL',1020,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','net_premiums_written','SUBTOTAL','MAIN',1030,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','net_premiums_earned','SUBTOTAL','MAIN',1040,NULL,1,'Top line for insurers'),
    -- Investment income (uses universal revenue mapping; insurers report it here)
    ('US_GAAP','insurance','income_statement','net_investment_income_insurance','WATERFALL','MAIN',1110,NULL,1,NULL),
    -- Losses and expenses
    ('US_GAAP','insurance','income_statement','claims_and_losses_incurred','WATERFALL','MAIN',1210,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','catastrophe_losses','DISCLOSURE','SUPPLEMENTAL',1215,'claims_and_losses_incurred',2,NULL),
    ('US_GAAP','insurance','income_statement','change_in_policy_benefit_reserves','WATERFALL','MAIN',1220,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','interest_credited_on_policyholder_account_balances','WATERFALL','MAIN',1230,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','policy_acquisition_costs','WATERFALL','MAIN',1240,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','insurance_underwriting_expense','WATERFALL','MAIN',1250,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','underwriting_income_loss','SUBTOTAL','MAIN',1300,NULL,1,'Premium - claims - underwriting expense'),
    -- Underwriting ratios
    ('US_GAAP','insurance','income_statement','loss_ratio','DISCLOSURE','MAIN',1310,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','expense_ratio_insurance_underwriting','DISCLOSURE','MAIN',1320,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','combined_ratio','DISCLOSURE','MAIN',1330,NULL,1,'Loss ratio + expense ratio'),
    -- Bottom line
    ('US_GAAP','insurance','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2010,NULL,1,NULL),
    -- JP-specific items hidden from US display
    ('US_GAAP','insurance','income_statement','increase_in_policy_reserves_japan','HIDDEN','HIDE',9001,NULL,1,NULL),
    ('US_GAAP','insurance','income_statement','maturity_refunds_japan_property_casualty','HIDDEN','HIDE',9002,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 7. US_GAAP reit income_statement
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('US_GAAP','reit','income_statement','rental_revenue','WATERFALL','MAIN',1010,NULL,1,'Primary REIT top line'),
    ('US_GAAP','reit','income_statement','straight_line_rent_adjustment','DISCLOSURE','SUPPLEMENTAL',1020,'rental_revenue',2,NULL),
    ('US_GAAP','reit','income_statement','property_operating_expenses','WATERFALL','MAIN',1110,NULL,1,NULL),
    -- Net operating income (REIT-defined; rolled up from rental_revenue - property_operating_expenses)
    ('US_GAAP','reit','income_statement','net_operating_income','CALCULATED','MAIN',1200,NULL,1,'NOI = rental revenue - property opex'),
    -- D&A separately for REIT presentation
    ('US_GAAP','reit','income_statement','total_depreciation_and_amortization','SUBTOTAL','MAIN',1300,NULL,1,NULL),
    -- Standard bottom line
    ('US_GAAP','reit','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,NULL),
    ('US_GAAP','reit','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('US_GAAP','reit','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('US_GAAP','reit','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    -- REIT-specific KPIs
    ('US_GAAP','reit','income_statement','funds_from_operations','CALCULATED','MAIN',2000,NULL,1,'FFO = net income + D&A + asset gain/loss'),
    ('US_GAAP','reit','income_statement','adjusted_funds_from_operations','CALCULATED','MAIN',2010,NULL,1,'AFFO = FFO - recurring capex'),
    ('US_GAAP','reit','income_statement','funds_from_operations_per_share','CALCULATED','MAIN',2020,NULL,1,NULL),
    ('US_GAAP','reit','income_statement','adjusted_funds_from_operations_per_share','CALCULATED','MAIN',2030,NULL,1,NULL),
    ('US_GAAP','reit','income_statement','distribution_per_unit_japan_reit','HIDDEN','HIDE',9001,NULL,1,'JP-specific');

-- ---------------------------------------------------------------------------
-- 8. US_GAAP asset_manager_other_financial income_statement
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Fee revenue
    ('US_GAAP','asset_manager_other_financial','income_statement','management_fee_revenue','WATERFALL','MAIN',1010,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','performance_fee_revenue','WATERFALL','MAIN',1020,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','revenue','SUBTOTAL','MAIN',1090,NULL,1,'Total revenue including non-fee items'),
    -- Operating expenses (use universal items)
    ('US_GAAP','asset_manager_other_financial','income_statement','total_operating_expenses','SUBTOTAL','MAIN',1190,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','selling_general_and_administrative_expense','DISCLOSURE','SUPPLEMENTAL',1200,'total_operating_expenses',2,NULL),
    -- Industry-specific intermediate
    ('US_GAAP','asset_manager_other_financial','income_statement','fee_related_earnings','CALCULATED','MAIN',1300,NULL,1,'FRE = fee revenue - operating expenses'),
    -- Bottom line
    ('US_GAAP','asset_manager_other_financial','income_statement','earnings_before_interest_taxes','SUBTOTAL','MAIN',1500,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1700,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','income_tax_provision','WATERFALL','MAIN',1800,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    -- Industry KPI
    ('US_GAAP','asset_manager_other_financial','income_statement','distributable_earnings','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2010,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2020,NULL,1,NULL),
    -- AUM disclosures
    ('US_GAAP','asset_manager_other_financial','income_statement','assets_under_management','DISCLOSURE','MAIN',2100,NULL,1,'AUM'),
    ('US_GAAP','asset_manager_other_financial','income_statement','fee_earning_assets_under_management','DISCLOSURE','MAIN',2110,NULL,1,'Fee-earning AUM'),
    ('US_GAAP','asset_manager_other_financial','income_statement','net_flows','DISCLOSURE','MAIN',2120,NULL,1,NULL),
    ('US_GAAP','asset_manager_other_financial','income_statement','dry_powder','DISCLOSURE','MAIN',2130,NULL,1,NULL);
