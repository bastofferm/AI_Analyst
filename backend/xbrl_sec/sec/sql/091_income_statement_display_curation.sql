-- Tighten default income-statement display curation.
--
-- The compact/default statement should show high-level investor-useful rows.
-- Detailed D&A components and below-operating components stay available as
-- supplemental/drilldown rows, but should not compete with the main bridge.

SET search_path TO sec, public;

UPDATE ref_std_statement_display_profile
   SET display_role = 'SUBTOTAL',
       display_policy = 'MAIN',
       display_order = 1450,
       display_parent_id = NULL,
       indent_level = 1,
       note = 'Compact D&A add-back row. Depreciation and amortization components remain supplemental children.',
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND line_item_id = 'total_depreciation_and_amortization';

UPDATE ref_std_statement_display_profile
   SET display_role = 'CALCULATED',
       display_policy = 'MAIN',
       display_order = 1460,
       display_parent_id = NULL,
       indent_level = 1,
       note = 'Calculated EBITDA bridge.',
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND line_item_id = 'earnings_before_interest_taxes_depreciation_amortization';

UPDATE ref_std_statement_display_profile
   SET display_role = 'DISCLOSURE',
       display_policy = 'SUPPLEMENTAL',
       display_parent_id = 'total_depreciation_and_amortization',
       indent_level = 2,
       note = 'D&A component detail; hidden from compact/default statement display.',
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND line_item_id IN ('depreciation', 'amortization_of_intangibles');

UPDATE ref_std_statement_display_profile
   SET display_role = 'DISCLOSURE',
       display_policy = 'SUPPLEMENTAL',
       display_parent_id = 'total_operating_expenses',
       indent_level = 2,
       note = 'Operating expense component detail; hidden from compact/default statement display.',
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND line_item_id IN (
       'research_and_development_expense',
       'selling_general_and_administrative_expense'
   );

UPDATE ref_std_statement_display_profile
   SET display_role = 'DISCLOSURE',
       display_policy = 'SUPPLEMENTAL',
       display_parent_id = 'total_non_operating_income_expense',
       indent_level = 2,
       note = 'Below-operating component detail; hidden from compact/default statement display.',
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND line_item_id IN (
       'interest_income',
       'interest_expense',
       'net_interest_expense',
       'equity_in_earnings_of_affiliates',
       'foreign_exchange_gain_loss',
       'non_operating_income'
   );
