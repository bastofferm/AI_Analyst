-- Residual cost_of_goods_sold for filers that tag cost-of-revenue with
-- extension concepts (e.g. ORCL orcl:* lines, CIK 0001341439, FY2019+).
--
-- Such filers report us-gaap:CostsAndExpenses (-> total_cost_and_expenses)
-- plus the standard opex face lines, but their cost-of-revenue lines live in
-- filer-extension namespaces the ingest never captures. Wiring the rollup
-- total_cost_and_expenses -> cogs + opex siblings lets graph_closure's
-- top-down pass derive cogs = total - known siblings (metric_type RESIDUAL)
-- when cogs is the only missing child.
--
-- The child set mirrors the income-statement face structure of the affected
-- cohort (FY2019-2025 simulation: 53 entity-years fire, derived gross
-- margins all sector-plausible). Deliberately tight: the exactly-one-missing
-- rule in close_graph means a looser set would silently absorb unlisted
-- expense lines into cogs.
--
-- Guards live in us_standardize._run_closure_pass: a synthesized
-- total_cost_and_expenses from this child set is dropped (a partial sum
-- missing cogs is garbage), and a non-negative cogs residual is dropped
-- (4/53 simulated entity-years had structurally non-conforming totals).

SET search_path TO sec, public;

INSERT INTO ref_std_item_edge
    (parent_id, child_id, sign, edge_type, statement_type,
     accounting_standard, sector_scope, sibling_rank, spec_source)
SELECT v.parent_id, v.child_id, v.sign, 'rollup', 'income_statement',
       'US_GAAP', 'corp', v.sibling_rank, '111_cogs_residual_edges'
FROM (VALUES
    ('total_cost_and_expenses', 'cost_of_goods_sold',                         -1, 1),
    ('total_cost_and_expenses', 'research_and_development_expense',           -1, 2),
    ('total_cost_and_expenses', 'selling_general_and_administrative_expense', -1, 3),
    ('total_cost_and_expenses', 'amortization_of_intangibles',                -1, 4),
    ('total_cost_and_expenses', 'special_gains_losses_japan_gaap',            -1, 5),
    ('total_cost_and_expenses', 'restructuring_charges',                      -1, 6)
) AS v(parent_id, child_id, sign, sibling_rank)
WHERE NOT EXISTS (
    SELECT 1 FROM ref_std_item_edge e
    WHERE e.parent_id = v.parent_id
      AND e.child_id = v.child_id
      AND e.edge_type = 'rollup'
      AND e.statement_type = 'income_statement'
      AND e.accounting_standard = 'US_GAAP'
      AND e.sector_scope = 'corp'
);

-- Matching rollup identity-check row. graph_closure's pre-derivation
-- consistency pass emits a violation keyed rollup:<parent>:<sector>:<std>
-- whenever a filer has the parent and all children filed but they do not
-- reconcile; ref_std_identity_violation.check_id has an FK to this table, so
-- the row must exist. registry_sync._rollup_check_rows normally synthesizes
-- this from the spec; we mirror it here since these edges are migration-sourced.
INSERT INTO ref_std_identity_check
    (check_id, description, statement_type, lhs_item_id,
     rhs_item_ids, rhs_signs, tolerance_bp, cross_statement,
     sector_scope, accounting_standard)
VALUES (
    'rollup:total_cost_and_expenses:corp:US_GAAP',
    'Rollup consistency: total_cost_and_expenses = Σ children',
    'income_statement',
    'total_cost_and_expenses',
    ARRAY['cost_of_goods_sold', 'research_and_development_expense',
          'selling_general_and_administrative_expense', 'amortization_of_intangibles',
          'special_gains_losses_japan_gaap', 'restructuring_charges'],
    ARRAY[-1, -1, -1, -1, -1, -1]::SMALLINT[],
    5,
    NULL,
    'corp',
    'US_GAAP'
)
ON CONFLICT (check_id) DO UPDATE SET
    rhs_item_ids = EXCLUDED.rhs_item_ids,
    rhs_signs    = EXCLUDED.rhs_signs;
