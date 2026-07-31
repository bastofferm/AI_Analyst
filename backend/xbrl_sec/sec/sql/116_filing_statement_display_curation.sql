-- Filing-native display curation bridge.
--
-- This is intentionally a narrow exception layer. The primary display
-- controls remain ref_std_statement_display_profile and
-- concept_target_display_policy; this table only handles filing/company
-- presentation exceptions that cannot be expressed there.

SET search_path TO sec, public;

ALTER TABLE fact_filing_statement_display_node
    DROP CONSTRAINT IF EXISTS fact_filing_statement_display_node_default_visibility_check;

ALTER TABLE fact_filing_statement_display_node
    ADD CONSTRAINT fact_filing_statement_display_node_default_visibility_check
    CHECK (default_visibility IN ('default', 'detail', 'supplemental', 'audit_only', 'hidden'));

CREATE TABLE IF NOT EXISTS ref_filing_statement_display_override (
    override_id                    BIGSERIAL PRIMARY KEY,
    jurisdiction                   TEXT NOT NULL CHECK (jurisdiction IN ('US', 'JP')),
    entity_id                      TEXT,
    filing_id                      TEXT,
    api_statement                  TEXT CHECK (api_statement IN ('BS', 'IS', 'CF')),
    statement_type                 TEXT CHECK (statement_type IN ('balance_sheet', 'income_statement', 'cash_flow_statement')),
    role_uri                       TEXT,
    source_concept_id              TEXT,
    std_line_item_id               TEXT,
    override_action                TEXT NOT NULL CHECK (override_action IN (
        'hide',
        'promote',
        'demote',
        'rename',
        'reparent',
        'bind_value',
        'order',
        'set_depth',
        'set_role',
        'visibility'
    )),
    display_label                  TEXT,
    display_parent_concept_id      TEXT,
    display_parent_std_line_item_id TEXT,
    value_binding_concept_id       TEXT,
    display_depth                  SMALLINT,
    display_order                  INTEGER,
    display_role                   TEXT,
    default_visibility             TEXT CHECK (default_visibility IN ('default', 'detail', 'supplemental', 'audit_only', 'hidden')),
    note                           TEXT,
    active                         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_concept_id IS NOT NULL OR std_line_item_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_rfsdo_lookup
    ON ref_filing_statement_display_override (
        jurisdiction,
        COALESCE(entity_id, ''),
        COALESCE(filing_id, ''),
        COALESCE(api_statement, ''),
        COALESCE(statement_type, ''),
        COALESCE(source_concept_id, ''),
        COALESCE(std_line_item_id, ''),
        active
    );

COMMENT ON TABLE ref_filing_statement_display_override IS
    'Narrow filing-native display exceptions applied after standardized display profile and concept-target policy.';

WITH seeds(
    source_concept_id,
    api_statement,
    override_action,
    display_label,
    value_binding_concept_id,
    default_visibility,
    note
) AS (
    VALUES
    ('us-gaap/AssetsAbstract', 'BS', 'rename', 'Assets', NULL, NULL, 'AAR compact balance-sheet group label.'),
    ('us-gaap/LiabilitiesAndStockholdersEquityAbstract', 'BS', 'rename', 'Liabilities and Equity', NULL, NULL, 'AAR compact balance-sheet group label.'),
    ('us-gaap/RevenuesAbstract', 'IS', 'rename', 'Revenue', NULL, NULL, 'AAR compact income-statement group label.'),
    ('us-gaap/CostsAndExpensesAbstract', 'IS', 'rename', 'Costs and expenses', NULL, NULL, 'AAR compact income-statement group label.'),
    ('air/OperatingIncomeLossIncludingIncomeLossFromEquityMethodInvestments', 'IS', 'rename', 'Operating income (loss)', NULL, NULL, 'AAR issuer-specific operating-income label.'),
    ('air/IncomeLossFromContinuingOperationsBeforeIncomeTaxesAndMinorityInterest', 'IS', 'rename', 'Income (loss) from continuing operations before income taxes', NULL, NULL, 'AAR issuer-specific pre-tax label.'),
    ('us-gaap/IncomeLossFromContinuingOperations', 'IS', 'rename', 'Income (loss) from continuing operations', NULL, NULL, 'AAR compact income-statement label.'),
    ('us-gaap/IncomeLossFromDiscontinuedOperationsNetOfTax', 'IS', 'rename', 'Income (loss) from discontinued operations, net of tax', NULL, NULL, 'AAR compact income-statement label.'),
    ('us-gaap/NetIncomeLoss', 'IS', 'rename', 'Net income (loss)', NULL, NULL, 'AAR compact income-statement label.'),
    ('us-gaap/EarningsPerShareBasicAbstract', 'IS', 'rename', 'Earnings per share, basic', NULL, NULL, 'AAR EPS group label.'),
    ('us-gaap/EarningsPerShareDilutedAbstract', 'IS', 'rename', 'Earnings per share, diluted', NULL, NULL, 'AAR EPS group label.'),
    ('us-gaap/OtherComprehensiveIncomeLossNetOfTaxPeriodIncreaseDecreaseAbstract', 'IS', 'rename', 'Other comprehensive income (loss), net of tax', NULL, 'detail', 'AAR OCI group is detail in the operations display.'),
    ('us-gaap/NetCashProvidedByUsedInOperatingActivitiesAbstract', 'CF', 'rename', 'Operating activities', NULL, NULL, 'AAR cash-flow group label.'),
    ('us-gaap/NetCashProvidedByUsedInInvestingActivitiesAbstract', 'CF', 'rename', 'Investing activities', NULL, NULL, 'AAR cash-flow group label.'),
    ('us-gaap/NetCashProvidedByUsedInFinancingActivitiesAbstract', 'CF', 'rename', 'Financing activities', NULL, NULL, 'AAR cash-flow group label.'),
    ('us-gaap/EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations', 'CF', 'rename', 'Effect of exchange rate changes on cash', NULL, NULL, 'AAR compact cash-flow label.'),
    ('us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect', 'CF', 'rename', 'Net change in cash and restricted cash', NULL, NULL, 'AAR compact cash-flow label.'),
    ('us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsIncludingDisposalGroupAndDiscontinuedOperations', 'CF', 'rename', 'Cash and restricted cash, end of period', NULL, NULL, 'AAR compact cash-flow label.'),

    ('us-gaap/AssetsAbstract', 'BS', 'bind_value', NULL, 'us-gaap/Assets', NULL, 'Bind abstract group to reported total.'),
    ('us-gaap/LiabilitiesAndStockholdersEquityAbstract', 'BS', 'bind_value', NULL, 'us-gaap/LiabilitiesAndStockholdersEquity', NULL, 'Bind abstract group to reported total.'),
    ('us-gaap/AssetsCurrentAbstract', 'BS', 'bind_value', NULL, 'us-gaap/AssetsCurrent', NULL, 'Bind abstract group to reported total.'),
    ('us-gaap/LiabilitiesCurrentAbstract', 'BS', 'bind_value', NULL, 'us-gaap/LiabilitiesCurrent', NULL, 'Bind abstract group to reported total.'),
    ('us-gaap/StockholdersEquityAbstract', 'BS', 'bind_value', NULL, 'us-gaap/StockholdersEquity', NULL, 'Bind abstract group to reported total.'),
    ('us-gaap/RevenuesAbstract', 'IS', 'bind_value', NULL, 'us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax', NULL, 'Bind abstract group to reported revenue.'),
    ('us-gaap/CostsAndExpensesAbstract', 'IS', 'bind_value', NULL, 'us-gaap/CostsAndExpenses', NULL, 'Bind abstract group to reported costs and expenses.'),
    ('us-gaap/EarningsPerShareBasicAbstract', 'IS', 'bind_value', NULL, 'us-gaap/EarningsPerShareBasic', NULL, 'Bind EPS group to reported basic EPS.'),
    ('us-gaap/EarningsPerShareDilutedAbstract', 'IS', 'bind_value', NULL, 'us-gaap/EarningsPerShareDiluted', NULL, 'Bind EPS group to reported diluted EPS.'),
    ('us-gaap/OtherComprehensiveIncomeLossNetOfTaxPeriodIncreaseDecreaseAbstract', 'IS', 'bind_value', NULL, 'us-gaap/OtherComprehensiveIncomeLossNetOfTax', 'detail', 'Bind OCI group to reported OCI.'),
    ('us-gaap/NetCashProvidedByUsedInOperatingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInOperatingActivities', NULL, 'Bind cash-flow group to reported subtotal.'),
    ('us-gaap/NetCashProvidedByUsedInOperatingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInOperatingActivitiesContinuingOperations', NULL, 'Fallback binding for continuing-operations cash-flow subtotal.'),
    ('us-gaap/NetCashProvidedByUsedInInvestingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInInvestingActivities', NULL, 'Bind cash-flow group to reported subtotal.'),
    ('us-gaap/NetCashProvidedByUsedInInvestingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInInvestingActivitiesContinuingOperations', NULL, 'Fallback binding for continuing-operations cash-flow subtotal.'),
    ('us-gaap/NetCashProvidedByUsedInFinancingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInFinancingActivities', NULL, 'Bind cash-flow group to reported subtotal.'),
    ('us-gaap/NetCashProvidedByUsedInFinancingActivitiesAbstract', 'CF', 'bind_value', NULL, 'us-gaap/NetCashProvidedByUsedInFinancingActivitiesContinuingOperations', NULL, 'Fallback binding for continuing-operations cash-flow subtotal.')
)
INSERT INTO ref_filing_statement_display_override
    (jurisdiction, entity_id, filing_id, source_concept_id, api_statement,
     override_action, display_label, value_binding_concept_id,
     default_visibility, note)
SELECT 'US',
       '0000001750',
       '0001104659-20-108360',
       source_concept_id,
       api_statement,
       override_action,
       display_label,
       value_binding_concept_id,
       default_visibility,
       note
FROM seeds
WHERE NOT EXISTS (
    SELECT 1
    FROM ref_filing_statement_display_override o
    WHERE o.jurisdiction = 'US'
      AND COALESCE(o.entity_id, '') = '0000001750'
      AND COALESCE(o.filing_id, '') = '0001104659-20-108360'
      AND COALESCE(o.source_concept_id, '') = seeds.source_concept_id
      AND COALESCE(o.api_statement, '') = seeds.api_statement
      AND o.override_action = seeds.override_action
      AND COALESCE(o.value_binding_concept_id, '') = COALESCE(seeds.value_binding_concept_id, '')
      AND o.active
);
