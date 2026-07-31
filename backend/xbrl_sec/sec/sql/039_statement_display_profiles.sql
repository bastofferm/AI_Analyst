-- Statement display profiles.
--
-- This layer controls presentation only. ref_standardized_line_items remains the
-- canonical line-item registry; this table defines how those canonical items are
-- rendered for a statement model profile.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS ref_std_statement_display_profile (
    accounting_standard TEXT NOT NULL
        CHECK (accounting_standard IN ('US_GAAP', 'JP_GAAP', 'IFRS')),
    sector_scope        TEXT NOT NULL,
    statement_type      TEXT NOT NULL
        CHECK (statement_type IN ('balance_sheet', 'income_statement', 'cash_flow_statement')),
    line_item_id        TEXT NOT NULL REFERENCES ref_standardized_line_items (line_item_id),
    display_role        TEXT NOT NULL
        CHECK (display_role IN ('WATERFALL', 'SUBTOTAL', 'TOTAL', 'CALCULATED', 'RESIDUAL', 'DISCLOSURE', 'HIDDEN')),
    display_policy      TEXT NOT NULL
        CHECK (display_policy IN ('MAIN', 'SUPPLEMENTAL', 'DRILLDOWN_ONLY', 'HIDE')),
    display_order       INTEGER,
    display_parent_id   TEXT,
    indent_level        SMALLINT NOT NULL DEFAULT 1,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (accounting_standard, sector_scope, statement_type, line_item_id)
);

COMMENT ON TABLE ref_std_statement_display_profile IS
    'Sector/accounting-standard-specific presentation profile for canonical standardized line items. Controls dashboard display, not mapping or standardization.';

CREATE INDEX IF NOT EXISTS idx_rssdp_scope
    ON ref_std_statement_display_profile (accounting_standard, sector_scope, statement_type, display_policy, display_order);

INSERT INTO ref_standardized_line_items
    (line_item_id, category, label, description, is_filed, importance, formula,
     mapping_sector, unit_type, statement_type, sector_scope, maps_into_metrics,
     registry_version, registry_source, item_class, derivation_policy,
     display_order_us_gaap, display_order_jp_gaap)
VALUES
    ('revenue_growth_year_over_year', 'income_statement', 'Revenue Growth',
     'Year-over-year revenue growth shown as a display-only income statement analytic row.',
     FALSE, 3, 'revenue_t / revenue_t1 - 1', 'corp', 'DEC', 'income_statement',
     'universal', ARRAY[]::TEXT[], 'display_profile_v1', '039_statement_display_profiles.sql',
     'supplemental', 'always_compute', 2020, NULL),
    ('net_income_growth_year_over_year', 'income_statement', 'Net Profit Growth',
     'Year-over-year net income / net profit growth shown as a display-only income statement analytic row.',
     FALSE, 3, 'net_income_t / net_income_t1 - 1', 'corp', 'DEC', 'income_statement',
     'universal', ARRAY[]::TEXT[], 'display_profile_v1', '039_statement_display_profiles.sql',
     'supplemental', 'always_compute', 2030, NULL),
    ('earnings_before_interest_taxes_growth_year_over_year', 'income_statement', 'EBIT Growth',
     'Year-over-year EBIT growth shown as a display-only income statement analytic row.',
     FALSE, 3, 'ebit_t / ebit_t1 - 1', 'corp', 'DEC', 'income_statement',
     'universal', ARRAY[]::TEXT[], 'display_profile_v1', '039_statement_display_profiles.sql',
     'supplemental', 'always_compute', 2040, NULL),
    ('earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year', 'income_statement', 'EBITDA Growth',
     'Year-over-year EBITDA growth shown as a display-only income statement analytic row.',
     FALSE, 3, 'ebitda_t / ebitda_t1 - 1', 'corp', 'DEC', 'income_statement',
     'universal', ARRAY[]::TEXT[], 'display_profile_v1', '039_statement_display_profiles.sql',
     'supplemental', 'always_compute', 2050, NULL)
ON CONFLICT (line_item_id)
DO UPDATE SET
    category = EXCLUDED.category,
    label = EXCLUDED.label,
    description = EXCLUDED.description,
    is_filed = EXCLUDED.is_filed,
    importance = EXCLUDED.importance,
    formula = EXCLUDED.formula,
    mapping_sector = EXCLUDED.mapping_sector,
    unit_type = EXCLUDED.unit_type,
    statement_type = EXCLUDED.statement_type,
    sector_scope = EXCLUDED.sector_scope,
    registry_version = EXCLUDED.registry_version,
    registry_source = EXCLUDED.registry_source,
    item_class = EXCLUDED.item_class,
    derivation_policy = EXCLUDED.derivation_policy,
    display_order_us_gaap = EXCLUDED.display_order_us_gaap,
    display_order_jp_gaap = EXCLUDED.display_order_jp_gaap,
    updated_at = now();

INSERT INTO ref_std_statement_display_profile
    (accounting_standard, sector_scope, statement_type, line_item_id,
     display_role, display_policy, display_order, display_parent_id, indent_level, note)
VALUES
    ('US_GAAP', 'corp', 'income_statement', 'revenue',
     'WATERFALL', 'MAIN', 1000, NULL, 1, 'Top-line reported revenue.'),
    ('US_GAAP', 'corp', 'income_statement', 'cost_of_goods_sold',
     'WATERFALL', 'MAIN', 1100, NULL, 1, 'Direct cost of revenue / cost of goods sold. Top-level row in the gross-profit bridge.'),
    ('US_GAAP', 'corp', 'income_statement', 'gross_profit',
     'SUBTOTAL', 'MAIN', 1200, NULL, 1, 'Revenue less cost of goods sold.'),
    ('US_GAAP', 'corp', 'income_statement', 'total_operating_expenses',
     'SUBTOTAL', 'MAIN', 1390, NULL, 1, 'Filed or modeled total operating expenses. Detail rows remain supplemental unless the profile has a clean decomposition.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_before_interest_taxes',
     'SUBTOTAL', 'MAIN', 1500, NULL, 1, 'Operating income / EBIT.'),
    ('US_GAAP', 'corp', 'income_statement', 'interest_income',
     'DISCLOSURE', 'SUPPLEMENTAL', 1610, 'total_non_operating_income_expense', 2, 'Below-operating component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'interest_expense',
     'DISCLOSURE', 'SUPPLEMENTAL', 1620, 'total_non_operating_income_expense', 2, 'Below-operating component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'net_interest_expense',
     'DISCLOSURE', 'SUPPLEMENTAL', 1630, 'total_non_operating_income_expense', 2, 'Below-operating residual detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'equity_in_earnings_of_affiliates',
     'DISCLOSURE', 'SUPPLEMENTAL', 1640, 'total_non_operating_income_expense', 2, 'Below-operating component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'foreign_exchange_gain_loss',
     'DISCLOSURE', 'SUPPLEMENTAL', 1650, 'total_non_operating_income_expense', 2, 'Below-operating component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'non_operating_income',
     'DISCLOSURE', 'SUPPLEMENTAL', 1660, 'total_non_operating_income_expense', 2, 'Below-operating component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'total_non_operating_income_expense',
     'SUBTOTAL', 'MAIN', 1690, NULL, 1, 'Total below-operating income / expense.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_before_taxes',
     'SUBTOTAL', 'MAIN', 1700, NULL, 1, 'Pre-tax income.'),
    ('US_GAAP', 'corp', 'income_statement', 'income_tax_provision',
     'WATERFALL', 'MAIN', 1800, NULL, 1, 'Income tax expense / benefit.'),
    ('US_GAAP', 'corp', 'income_statement', 'net_income',
     'TOTAL', 'MAIN', 1900, NULL, 1, 'Net income.'),
    ('US_GAAP', 'corp', 'income_statement', 'net_income_attributable_to_common',
     'TOTAL', 'MAIN', 1910, NULL, 1, 'Net income available to common shareholders; EPS numerator where available.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_per_share_basic',
     'CALCULATED', 'MAIN', 2000, NULL, 1, 'Basic earnings per share.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_per_share_diluted',
     'CALCULATED', 'MAIN', 2010, NULL, 1, 'Diluted earnings per share.'),
    ('US_GAAP', 'corp', 'income_statement', 'revenue_growth_year_over_year',
     'CALCULATED', 'MAIN', 2020, NULL, 1, 'Revenue year-over-year growth displayed as a percentage.'),
    ('US_GAAP', 'corp', 'income_statement', 'net_income_growth_year_over_year',
     'CALCULATED', 'MAIN', 2030, NULL, 1, 'Net profit year-over-year growth displayed as a percentage.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_before_interest_taxes_growth_year_over_year',
     'CALCULATED', 'MAIN', 2040, NULL, 1, 'EBIT year-over-year growth displayed as a percentage.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_before_interest_taxes_depreciation_amortization_growth_year_over_year',
     'CALCULATED', 'MAIN', 2050, NULL, 1, 'EBITDA year-over-year growth displayed as a percentage.'),
    ('US_GAAP', 'corp', 'income_statement', 'shares_outstanding_basic',
     'CALCULATED', 'MAIN', 2100, NULL, 1, 'Weighted-average basic shares.'),
    ('US_GAAP', 'corp', 'income_statement', 'shares_outstanding_diluted',
     'CALCULATED', 'MAIN', 2110, NULL, 1, 'Weighted-average diluted shares.'),
    ('US_GAAP', 'corp', 'income_statement', 'dividends_per_share',
     'CALCULATED', 'MAIN', 2120, NULL, 1, 'Dividends per common share.'),
    ('US_GAAP', 'corp', 'income_statement', 'earnings_before_interest_taxes_depreciation_amortization',
     'CALCULATED', 'MAIN', 1460, NULL, 1, 'Calculated EBITDA bridge.'),

    ('US_GAAP', 'corp', 'income_statement', 'depreciation',
     'DISCLOSURE', 'SUPPLEMENTAL', 3000, 'total_depreciation_and_amortization', 2, 'D&A component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'amortization_of_intangibles',
     'DISCLOSURE', 'SUPPLEMENTAL', 3010, 'total_depreciation_and_amortization', 2, 'D&A component detail; hidden from compact/default statement display.'),
    ('US_GAAP', 'corp', 'income_statement', 'total_depreciation_and_amortization',
     'SUBTOTAL', 'MAIN', 1450, NULL, 1, 'Compact D&A add-back row. Depreciation and amortization components remain supplemental children.'),
    ('US_GAAP', 'corp', 'income_statement', 'research_and_development_expense',
     'DISCLOSURE', 'SUPPLEMENTAL', 3030, NULL, 1, 'Often a child of total operating expenses or an isolated disclosure; not additive in the first-pass main waterfall.'),
    ('US_GAAP', 'corp', 'income_statement', 'selling_general_and_administrative_expense',
     'DISCLOSURE', 'SUPPLEMENTAL', 3040, NULL, 1, 'Shown as supplemental until SG&A can be separated from total operating expenses without double counting.'),
    ('US_GAAP', 'corp', 'income_statement', 'labor_and_employee_costs',
     'DISCLOSURE', 'SUPPLEMENTAL', 3050, NULL, 1, 'Compensation/pension detail; not additive by default.'),
    ('US_GAAP', 'corp', 'income_statement', 'rent_and_lease_expense',
     'DISCLOSURE', 'SUPPLEMENTAL', 3060, NULL, 1, 'Lease/rent detail; not additive by default.'),
    ('US_GAAP', 'corp', 'income_statement', 'restructuring_charges',
     'DISCLOSURE', 'SUPPLEMENTAL', 3070, NULL, 1, 'Separately disclosed charge; avoid double counting unless modeled.'),
    ('US_GAAP', 'corp', 'income_statement', 'asset_impairment',
     'DISCLOSURE', 'SUPPLEMENTAL', 3080, NULL, 1, 'Separately disclosed impairment; avoid double counting unless modeled.'),
    ('US_GAAP', 'corp', 'income_statement', 'other_operating_income_expense_net',
     'DISCLOSURE', 'SUPPLEMENTAL', 3090, NULL, 1, 'Operating other detail; not part of first-pass clean corp waterfall.'),

    ('US_GAAP', 'corp', 'income_statement', 'special_gains_losses_japan_gaap',
     'HIDDEN', 'HIDE', 9000, NULL, 1, 'JP_GAAP-only row; hidden in US_GAAP corp profile.'),
    ('US_GAAP', 'corp', 'income_statement', 'non_operating_expenses_jp',
     'HIDDEN', 'HIDE', 9010, NULL, 1, 'JP_GAAP-only row; hidden in US_GAAP corp profile.')
ON CONFLICT (accounting_standard, sector_scope, statement_type, line_item_id)
DO UPDATE SET
    display_role = EXCLUDED.display_role,
    display_policy = EXCLUDED.display_policy,
    display_order = EXCLUDED.display_order,
    display_parent_id = EXCLUDED.display_parent_id,
    indent_level = EXCLUDED.indent_level,
    note = EXCLUDED.note,
    updated_at = now();
