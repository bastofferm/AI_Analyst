-- JP_GAAP display profiles, mirroring the US profiles in migration 097
-- but using the JP-specific intermediate concepts where they exist:
--   - ordinary_income_japan_gaap   (intermediate before special items)
--   - non_operating_expenses_jp    (JP non-operating subtotal)
--   - special_gains_losses_japan_gaap (subtotal for JP special items)
--   - ordinary_revenue_bank_japan_gaap / ordinary_expenses_bank_jp
--   - net_business_profit_bank_japan_gaap / gross_banking_profit
--   - distribution_per_unit_japan_reit
--   - increase_in_policy_reserves_japan / maturity_refunds_japan_property_casualty

SET search_path TO sec, public;

DELETE FROM ref_std_statement_display_profile
WHERE accounting_standard = 'JP_GAAP'
  AND ((sector_scope, statement_type) IN (
        ('corp', 'income_statement'),
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
-- 1. JP_GAAP corp income_statement (sales -> operating -> ordinary -> special)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('JP_GAAP','corp','income_statement','revenue','WATERFALL','MAIN',1000,NULL,1,'Net sales (売上高)'),
    ('JP_GAAP','corp','income_statement','cost_of_goods_sold','WATERFALL','MAIN',1100,NULL,1,'Cost of sales (売上原価)'),
    ('JP_GAAP','corp','income_statement','gross_profit','SUBTOTAL','MAIN',1200,NULL,1,'Gross profit (売上総利益)'),
    ('JP_GAAP','corp','income_statement','selling_general_and_administrative_expense','DISCLOSURE','SUPPLEMENTAL',1400,'total_operating_expenses',2,NULL),
    ('JP_GAAP','corp','income_statement','research_and_development_expense','DISCLOSURE','SUPPLEMENTAL',1410,'total_operating_expenses',2,NULL),
    ('JP_GAAP','corp','income_statement','labor_and_employee_costs','DISCLOSURE','SUPPLEMENTAL',1420,'total_operating_expenses',2,NULL),
    ('JP_GAAP','corp','income_statement','directors_bonus_japan_gaap','DISCLOSURE','SUPPLEMENTAL',1430,'total_operating_expenses',2,NULL),
    ('JP_GAAP','corp','income_statement','total_operating_expenses','SUBTOTAL','MAIN',1490,NULL,1,'SG&A (販売費及び一般管理費)'),
    ('JP_GAAP','corp','income_statement','earnings_before_interest_taxes','SUBTOTAL','MAIN',1500,NULL,1,'Operating income (営業利益)'),
    ('JP_GAAP','corp','income_statement','dividend_income_jp','DISCLOSURE','SUPPLEMENTAL',1610,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','interest_income','DISCLOSURE','SUPPLEMENTAL',1620,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','foreign_exchange_gain_loss','DISCLOSURE','SUPPLEMENTAL',1630,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','equity_in_earnings_of_affiliates','DISCLOSURE','SUPPLEMENTAL',1640,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','non_operating_income','DISCLOSURE','SUPPLEMENTAL',1650,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','interest_expense','DISCLOSURE','SUPPLEMENTAL',1660,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','non_operating_expenses_jp','DISCLOSURE','SUPPLEMENTAL',1670,'total_non_operating_income_expense',2,NULL),
    ('JP_GAAP','corp','income_statement','total_non_operating_income_expense','SUBTOTAL','MAIN',1690,NULL,1,'Net non-operating income/expense (営業外収益・費用)'),
    ('JP_GAAP','corp','income_statement','ordinary_income_japan_gaap','SUBTOTAL','MAIN',1700,NULL,1,'Ordinary income (経常利益)'),
    ('JP_GAAP','corp','income_statement','asset_impairment','DISCLOSURE','SUPPLEMENTAL',1710,'special_gains_losses_japan_gaap',2,NULL),
    ('JP_GAAP','corp','income_statement','restructuring_charges','DISCLOSURE','SUPPLEMENTAL',1720,'special_gains_losses_japan_gaap',2,NULL),
    ('JP_GAAP','corp','income_statement','special_gains_losses_japan_gaap','SUBTOTAL','MAIN',1790,NULL,1,'Special gains/losses (特別利益・損失)'),
    ('JP_GAAP','corp','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,'Income before tax (税引前当期純利益)'),
    ('JP_GAAP','corp','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,'Income taxes (法人税等)'),
    ('JP_GAAP','corp','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,'Net income (当期純利益)'),
    ('JP_GAAP','corp','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,'Net income attrib. to parent shareholders'),
    ('JP_GAAP','corp','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('JP_GAAP','corp','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2010,NULL,1,NULL),
    ('JP_GAAP','corp','income_statement','dividends_per_share','CALCULATED','MAIN',2020,NULL,1,NULL),
    ('JP_GAAP','corp','income_statement','revenue_growth_year_over_year','CALCULATED','MAIN',2030,NULL,1,NULL),
    ('JP_GAAP','corp','income_statement','net_income_growth_year_over_year','CALCULATED','MAIN',2040,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 2. JP_GAAP corp balance_sheet (mirror US corp)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
SELECT 'JP_GAAP', sector_scope, statement_type, line_item_id,
       display_role, display_policy, display_order, display_parent_id, indent_level, note
FROM ref_std_statement_display_profile
WHERE accounting_standard='US_GAAP' AND sector_scope='corp' AND statement_type='balance_sheet';

-- ---------------------------------------------------------------------------
-- 3. JP_GAAP corp cash_flow_statement (mirror US corp)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
SELECT 'JP_GAAP', sector_scope, statement_type, line_item_id,
       display_role, display_policy, display_order, display_parent_id, indent_level, note
FROM ref_std_statement_display_profile
WHERE accounting_standard='US_GAAP' AND sector_scope='corp' AND statement_type='cash_flow_statement';

-- ---------------------------------------------------------------------------
-- 4. JP_GAAP bank_financial income_statement
--    JP banks report in the ordinary-revenue / ordinary-expense / net-business-profit structure.
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    -- Ordinary revenue block
    ('JP_GAAP','bank_financial','income_statement','interest_on_loans_jp','DISCLOSURE','SUPPLEMENTAL',1010,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','interest_dividends_securities_jp','DISCLOSURE','SUPPLEMENTAL',1020,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','interest_call_loans_jp','DISCLOSURE','SUPPLEMENTAL',1030,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','interest_due_from_banks_jp','DISCLOSURE','SUPPLEMENTAL',1040,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_fund_management_jp','DISCLOSURE','SUPPLEMENTAL',1050,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','remittance_fees_jp','DISCLOSURE','SUPPLEMENTAL',1060,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_fee_income_jp','DISCLOSURE','SUPPLEMENTAL',1070,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','bond_sale_gain_jp','DISCLOSURE','SUPPLEMENTAL',1080,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','fx_trading_gain_jp','DISCLOSURE','SUPPLEMENTAL',1085,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_business_income_misc_jp','DISCLOSURE','SUPPLEMENTAL',1086,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_business_income_jp','DISCLOSURE','SUPPLEMENTAL',1087,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_ordinary_income_jp','DISCLOSURE','SUPPLEMENTAL',1088,'ordinary_revenue_bank_japan_gaap',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','ordinary_revenue_bank_japan_gaap','SUBTOTAL','MAIN',1090,NULL,1,'Ordinary revenue (経常収益)'),
    -- Ordinary expense block
    ('JP_GAAP','bank_financial','income_statement','fee_expense_jp','DISCLOSURE','SUPPLEMENTAL',1110,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','non_personnel_expense_jp','DISCLOSURE','SUPPLEMENTAL',1120,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','local_tax_bank_jp','DISCLOSURE','SUPPLEMENTAL',1130,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','bond_sale_loss_jp','DISCLOSURE','SUPPLEMENTAL',1140,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','fx_trading_loss_jp','DISCLOSURE','SUPPLEMENTAL',1145,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','trading_expense_jp','DISCLOSURE','SUPPLEMENTAL',1150,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_business_expense_misc_jp','DISCLOSURE','SUPPLEMENTAL',1160,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_business_expense_jp','DISCLOSURE','SUPPLEMENTAL',1170,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','other_ordinary_expense_jp','DISCLOSURE','SUPPLEMENTAL',1180,'ordinary_expenses_bank_jp',2,NULL),
    ('JP_GAAP','bank_financial','income_statement','ordinary_expenses_bank_jp','SUBTOTAL','MAIN',1190,NULL,1,'Ordinary expenses (経常費用)'),
    -- Business profits
    ('JP_GAAP','bank_financial','income_statement','gross_banking_profit','SUBTOTAL','MAIN',1200,NULL,1,'Gross banking profit (業務粗利益)'),
    ('JP_GAAP','bank_financial','income_statement','provision_for_loan_losses','WATERFALL','MAIN',1300,NULL,1,'Provision (貸倒引当金繰入)'),
    ('JP_GAAP','bank_financial','income_statement','net_business_profit_bank_japan_gaap','SUBTOTAL','MAIN',1400,NULL,1,'Net business profit (業務純益)'),
    -- Bottom line
    ('JP_GAAP','bank_financial','income_statement','ordinary_income_japan_gaap','SUBTOTAL','MAIN',1700,NULL,1,'Ordinary income (経常利益)'),
    ('JP_GAAP','bank_financial','income_statement','special_gains_losses_japan_gaap','SUBTOTAL','MAIN',1790,NULL,1,'Special items (特別損益)'),
    ('JP_GAAP','bank_financial','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,'Income before tax (税引前当期純利益)'),
    ('JP_GAAP','bank_financial','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2010,NULL,1,NULL),
    -- Hide the English-only bank items from JP rendering (have their own US profile)
    ('JP_GAAP','bank_financial','income_statement','total_interest_income','HIDDEN','HIDE',9001,NULL,1,'US-specific subtotal'),
    ('JP_GAAP','bank_financial','income_statement','total_interest_expense','HIDDEN','HIDE',9002,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','net_interest_income','HIDDEN','HIDE',9003,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','net_interest_income_after_provision','HIDDEN','HIDE',9004,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','non_interest_income','HIDDEN','HIDE',9005,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','non_interest_expense','HIDDEN','HIDE',9006,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','pre_provision_net_revenue','HIDDEN','HIDE',9007,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','fdic_insurance_expense','HIDDEN','HIDE',9008,NULL,1,'US bank-specific (FDIC)'),
    ('JP_GAAP','bank_financial','income_statement','amortization_of_core_deposit_intangibles','HIDDEN','HIDE',9009,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','tangible_book_value_per_share','HIDDEN','HIDE',9010,NULL,1,'US bank KPI'),
    ('JP_GAAP','bank_financial','income_statement','nonperforming_loan_ratio','HIDDEN','HIDE',9011,NULL,1,NULL),
    ('JP_GAAP','bank_financial','income_statement','net_charge_offs','HIDDEN','HIDE',9012,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 5. JP_GAAP bank_financial balance_sheet  (mirror US bank BS)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
SELECT 'JP_GAAP', sector_scope, statement_type, line_item_id,
       display_role, display_policy, display_order, display_parent_id, indent_level, note
FROM ref_std_statement_display_profile
WHERE accounting_standard='US_GAAP' AND sector_scope='bank_financial' AND statement_type='balance_sheet';

-- ---------------------------------------------------------------------------
-- 6. JP_GAAP bank_financial cash_flow_statement  (mirror US bank CF)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
SELECT 'JP_GAAP', sector_scope, statement_type, line_item_id,
       display_role, display_policy, display_order, display_parent_id, indent_level, note
FROM ref_std_statement_display_profile
WHERE accounting_standard='US_GAAP' AND sector_scope='bank_financial' AND statement_type='cash_flow_statement';

-- ---------------------------------------------------------------------------
-- 7. JP_GAAP insurance income_statement (uses Japan-specific reserve items)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('JP_GAAP','insurance','income_statement','gross_premiums_written','DISCLOSURE','SUPPLEMENTAL',1010,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','ceded_premiums_written','DISCLOSURE','SUPPLEMENTAL',1020,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','net_premiums_written','SUBTOTAL','MAIN',1030,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','net_premiums_earned','SUBTOTAL','MAIN',1040,NULL,1,'Earned premiums (正味収入保険料)'),
    ('JP_GAAP','insurance','income_statement','net_investment_income_insurance','WATERFALL','MAIN',1110,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','claims_and_losses_incurred','WATERFALL','MAIN',1210,NULL,1,'Claims (保険金支払)'),
    ('JP_GAAP','insurance','income_statement','catastrophe_losses','DISCLOSURE','SUPPLEMENTAL',1215,'claims_and_losses_incurred',2,NULL),
    ('JP_GAAP','insurance','income_statement','maturity_refunds_japan_property_casualty','WATERFALL','MAIN',1220,NULL,1,'Maturity refunds (満期返戻金)'),
    ('JP_GAAP','insurance','income_statement','increase_in_policy_reserves_japan','WATERFALL','MAIN',1230,NULL,1,'Policy reserves increase (責任準備金繰入)'),
    ('JP_GAAP','insurance','income_statement','change_in_policy_benefit_reserves','DISCLOSURE','SUPPLEMENTAL',1235,'increase_in_policy_reserves_japan',2,NULL),
    ('JP_GAAP','insurance','income_statement','interest_credited_on_policyholder_account_balances','WATERFALL','MAIN',1240,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','policy_acquisition_costs','WATERFALL','MAIN',1250,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','insurance_underwriting_expense','WATERFALL','MAIN',1260,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','underwriting_income_loss','SUBTOTAL','MAIN',1300,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','loss_ratio','DISCLOSURE','MAIN',1310,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','expense_ratio_insurance_underwriting','DISCLOSURE','MAIN',1320,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','combined_ratio','DISCLOSURE','MAIN',1330,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','ordinary_income_japan_gaap','SUBTOTAL','MAIN',1700,NULL,1,'Ordinary income (経常利益)'),
    ('JP_GAAP','insurance','income_statement','special_gains_losses_japan_gaap','SUBTOTAL','MAIN',1790,NULL,1,'Special items'),
    ('JP_GAAP','insurance','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','net_income_attributable_to_common','TOTAL','MAIN',1910,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','earnings_per_share_basic','CALCULATED','MAIN',2000,NULL,1,NULL),
    ('JP_GAAP','insurance','income_statement','earnings_per_share_diluted','CALCULATED','MAIN',2010,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 8. JP_GAAP reit income_statement (J-REIT distribution structure)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('JP_GAAP','reit','income_statement','rental_revenue','WATERFALL','MAIN',1010,NULL,1,'Rental revenue (賃貸収入)'),
    ('JP_GAAP','reit','income_statement','straight_line_rent_adjustment','DISCLOSURE','SUPPLEMENTAL',1020,'rental_revenue',2,NULL),
    ('JP_GAAP','reit','income_statement','property_operating_expenses','WATERFALL','MAIN',1110,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','net_operating_income','CALCULATED','MAIN',1200,NULL,1,'NOI'),
    ('JP_GAAP','reit','income_statement','total_depreciation_and_amortization','SUBTOTAL','MAIN',1300,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','ordinary_income_japan_gaap','SUBTOTAL','MAIN',1700,NULL,1,'Ordinary income'),
    ('JP_GAAP','reit','income_statement','special_gains_losses_japan_gaap','SUBTOTAL','MAIN',1790,NULL,1,'Special items'),
    ('JP_GAAP','reit','income_statement','earnings_before_taxes','SUBTOTAL','MAIN',1800,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','income_tax_provision','WATERFALL','MAIN',1850,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','net_income','TOTAL','MAIN',1900,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','distribution_per_unit_japan_reit','CALCULATED','MAIN',2000,NULL,1,'Distribution per unit (一口当たり分配金)'),
    ('JP_GAAP','reit','income_statement','funds_from_operations','CALCULATED','MAIN',2010,NULL,1,'FFO'),
    ('JP_GAAP','reit','income_statement','adjusted_funds_from_operations','CALCULATED','MAIN',2020,NULL,1,'AFFO'),
    ('JP_GAAP','reit','income_statement','funds_from_operations_per_share','CALCULATED','MAIN',2030,NULL,1,NULL),
    ('JP_GAAP','reit','income_statement','adjusted_funds_from_operations_per_share','CALCULATED','MAIN',2040,NULL,1,NULL);

-- ---------------------------------------------------------------------------
-- 9. JP_GAAP asset_manager_other_financial income_statement (mirror US)
-- ---------------------------------------------------------------------------

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
SELECT 'JP_GAAP', sector_scope, statement_type, line_item_id,
       display_role, display_policy, display_order, display_parent_id, indent_level, note
FROM ref_std_statement_display_profile
WHERE accounting_standard='US_GAAP' AND sector_scope='asset_manager_other_financial' AND statement_type='income_statement';
