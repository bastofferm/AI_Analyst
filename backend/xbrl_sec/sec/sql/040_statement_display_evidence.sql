-- Filing-level statement display evidence.
--
-- This table is intentionally separate from concept mappings and standardized
-- facts. It records how raw XBRL hierarchy evidence supports dashboard display
-- roles such as operating expense subtotal vs. supplemental cost disclosure.

SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_statement_display_evidence_us (
    cik                                TEXT NOT NULL,
    jurisdiction                       TEXT NOT NULL DEFAULT 'US'
                                           CHECK (jurisdiction = 'US'),
    fiscal_year                        SMALLINT NOT NULL,
    fiscal_period                      TEXT NOT NULL,
    period_end                         DATE,
    filing_id                          TEXT NOT NULL DEFAULT '',
    line_item_id                       TEXT NOT NULL
                                           REFERENCES ref_standardized_line_items (line_item_id),
    source_concept_id                  TEXT NOT NULL,
    source_concept_label               TEXT,
    statement_type                     TEXT NOT NULL,
    accounting_standard                TEXT NOT NULL DEFAULT 'US_GAAP',
    sector_scope                       TEXT NOT NULL DEFAULT 'corp',
    display_role                       TEXT NOT NULL
                                           CHECK (display_role IN (
                                               'OPERATING_EXPENSE_TOTAL',
                                               'OPERATING_EXPENSE_COMPONENT',
                                               'NATURE_DISCLOSURE',
                                               'NON_OPERATING_OR_OTHER',
                                               'AMBIGUOUS'
                                           )),
    evidence_quality                   TEXT NOT NULL
                                           CHECK (evidence_quality IN ('STRONG', 'MODERATE', 'WEAK')),
    role_reason                        TEXT,
    presentation_parent_id             TEXT,
    presentation_level                 SMALLINT,
    presentation_order                 INTEGER,
    presentation_position              INTEGER,
    calculation_parent_id              TEXT,
    calculation_root_id                TEXT,
    concept_path                       TEXT,
    weight                             NUMERIC,
    effective_weight                   NUMERIC,
    value                              NUMERIC,
    currency                           TEXT,
    mapping_id                         BIGINT,
    operating_reconciliation_delta     NUMERIC,
    operating_reconciliation_status    TEXT
                                           CHECK (operating_reconciliation_status IN ('PASS', 'FAIL', 'MISSING')),
    created_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cik, fiscal_year, fiscal_period, filing_id, line_item_id, source_concept_id)
);

UPDATE fact_statement_display_evidence_us
   SET filing_id = ''
 WHERE filing_id IS NULL;

ALTER TABLE fact_statement_display_evidence_us
    ALTER COLUMN filing_id SET DEFAULT '',
    ALTER COLUMN filing_id SET NOT NULL;

ALTER TABLE fact_statement_display_evidence_us
    DROP CONSTRAINT IF EXISTS fact_statement_display_evidence_us_pkey;

ALTER TABLE fact_statement_display_evidence_us
    ADD PRIMARY KEY (cik, fiscal_year, fiscal_period, filing_id, line_item_id, source_concept_id);

COMMENT ON TABLE fact_statement_display_evidence_us IS
    'Filing-level display evidence derived from raw XBRL hierarchy fields. Does not govern semantic concept mappings.';

CREATE INDEX IF NOT EXISTS idx_fsde_us_role
    ON fact_statement_display_evidence_us (sector_scope, statement_type, display_role);

CREATE INDEX IF NOT EXISTS idx_fsde_us_filing
    ON fact_statement_display_evidence_us (cik, filing_id, fiscal_year, fiscal_period);

CREATE OR REPLACE VIEW vw_us_corp_operating_cost_audit AS
SELECT
    source_concept_id,
    line_item_id,
    display_role,
    evidence_quality,
    COUNT(*) AS evidence_rows,
    COUNT(DISTINCT cik) AS entity_count,
    COUNT(DISTINCT filing_id) AS filing_count,
    COUNT(*) FILTER (WHERE operating_reconciliation_status = 'PASS') AS reconciliation_pass_rows,
    COUNT(*) FILTER (WHERE operating_reconciliation_status = 'FAIL') AS reconciliation_fail_rows,
    MIN(operating_reconciliation_delta) AS min_operating_reconciliation_delta,
    MAX(operating_reconciliation_delta) AS max_operating_reconciliation_delta,
    (array_agg(DISTINCT presentation_parent_id) FILTER (WHERE presentation_parent_id IS NOT NULL))[1:8]
        AS common_presentation_parents,
    (array_agg(DISTINCT calculation_parent_id) FILTER (WHERE calculation_parent_id IS NOT NULL))[1:8]
        AS common_calculation_parents,
    (array_agg(DISTINCT concept_path) FILTER (WHERE concept_path IS NOT NULL))[1:8]
        AS sample_concept_paths,
    (array_agg(DISTINCT role_reason) FILTER (WHERE role_reason IS NOT NULL))[1:5]
        AS role_reasons
FROM fact_statement_display_evidence_us
WHERE sector_scope = 'corp'
  AND statement_type = 'income_statement'
GROUP BY source_concept_id, line_item_id, display_role, evidence_quality;

UPDATE ref_std_statement_display_profile
   SET display_parent_id = 'total_operating_expenses',
       indent_level = 2,
       updated_at = now()
 WHERE accounting_standard = 'US_GAAP'
   AND sector_scope = 'corp'
   AND statement_type = 'income_statement'
   AND display_policy = 'SUPPLEMENTAL'
   AND line_item_id IN (
       'research_and_development_expense',
       'selling_general_and_administrative_expense',
       'depreciation',
       'amortization_of_intangibles',
       'total_depreciation_and_amortization',
       'labor_and_employee_costs',
       'rent_and_lease_expense',
       'restructuring_charges',
       'asset_impairment',
       'other_operating_income_expense_net'
   );
