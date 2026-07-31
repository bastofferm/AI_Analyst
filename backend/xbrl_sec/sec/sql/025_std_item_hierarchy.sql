-- Statement hierarchy: edge model, identity checks, and violation tracking.
--
-- Adds item_class / derivation_policy to ref_standardized_line_items,
-- creates ref_std_item_edge (one row per parent→child relationship across IS/BS/CF),
-- ref_std_identity_check (BS accounting equation, CF cash bridge, section identities),
-- and ref_std_identity_violation (per-entity audit trail).

SET search_path TO sec, public;

-- ---------------------------------------------------------------------------
-- Widen metric_type columns to accommodate derived metric type names
-- ---------------------------------------------------------------------------

ALTER TABLE fact_fundamentals_std_us
    ALTER COLUMN metric_type TYPE VARCHAR(32);

ALTER TABLE fact_fundamentals_std_jp
    ALTER COLUMN metric_type TYPE VARCHAR(32);

-- ---------------------------------------------------------------------------
-- Extend ref_standardized_line_items
-- ---------------------------------------------------------------------------

ALTER TABLE ref_standardized_line_items
    ADD COLUMN IF NOT EXISTS item_class TEXT,
    -- 'leaf' | 'intermediate' | 'catch_all' | 'supplemental' | 'cross_statement_ref'
    ADD COLUMN IF NOT EXISTS derivation_policy TEXT NOT NULL DEFAULT 'prefer_filed';
    -- 'prefer_filed' | 'always_compute' | 'residual'

COMMENT ON COLUMN ref_standardized_line_items.item_class IS
    'Position in the hierarchy: leaf (directly filed), intermediate (sum of children), catch_all (residual), supplemental (no rollup), cross_statement_ref.';
COMMENT ON COLUMN ref_standardized_line_items.derivation_policy IS
    'prefer_filed: use filed value when present, compute only when missing. always_compute: always recompute from children (validation). residual: derive top-down only.';

-- ---------------------------------------------------------------------------
-- ref_std_item_edge
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_std_item_edge (
    edge_id             BIGSERIAL PRIMARY KEY,
    parent_id           TEXT NOT NULL,
    child_id            TEXT NOT NULL,
    sign                SMALLINT NOT NULL DEFAULT 1
                            CHECK (sign IN (-1, 1)),
    edge_type           TEXT NOT NULL DEFAULT 'rollup'
                            CHECK (edge_type IN ('rollup', 'identity_check', 'cross_statement')),
    statement_type      TEXT NOT NULL
                            CHECK (statement_type IN ('balance_sheet', 'income_statement', 'cash_flow_statement')),
    accounting_standard TEXT
                            CHECK (accounting_standard IN ('US_GAAP', 'JP_GAAP', 'IFRS') OR accounting_standard IS NULL),
    sector_scope        TEXT NOT NULL DEFAULT 'universal',
    sibling_rank        SMALLINT NOT NULL DEFAULT 1,
    spec_source         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_std_item_edge IS
    'Parent-child edges for standardized line item hierarchy across IS, BS, and CF. sign=+1 means child adds to parent; sign=-1 means child is subtracted (contra-assets, treasury stock, capex).';
COMMENT ON COLUMN ref_std_item_edge.edge_type IS
    'rollup: normal summation edge. identity_check: cross-section constraint. cross_statement: link between items on different statements.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_rsie ON ref_std_item_edge
    (parent_id, child_id, COALESCE(accounting_standard, ''), sector_scope);

CREATE INDEX IF NOT EXISTS idx_rsie_parent ON ref_std_item_edge (parent_id, statement_type, sector_scope);
CREATE INDEX IF NOT EXISTS idx_rsie_child  ON ref_std_item_edge (child_id,  statement_type, sector_scope);
CREATE INDEX IF NOT EXISTS idx_rsie_stmt   ON ref_std_item_edge (statement_type, sector_scope, accounting_standard);

-- ---------------------------------------------------------------------------
-- ref_std_identity_check
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_std_identity_check (
    check_id            TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    statement_type      TEXT NOT NULL,
    lhs_item_id         TEXT NOT NULL,
    rhs_item_ids        TEXT[]     NOT NULL,
    rhs_signs           SMALLINT[] NOT NULL,
    tolerance_bp        SMALLINT   NOT NULL DEFAULT 1,
    cross_statement     JSONB,
    sector_scope        TEXT NOT NULL DEFAULT 'universal',
    accounting_standard TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_std_identity_check IS
    'Named accounting identities to verify after the derivation pass: BS accounting equation, CF net-change identity, CF cash bridge to BS.';
COMMENT ON COLUMN ref_std_identity_check.tolerance_bp IS
    'Allowable absolute rounding gap as basis points of the lhs value (1 bp = 0.01 %). Differences within tolerance are not flagged.';
COMMENT ON COLUMN ref_std_identity_check.cross_statement IS
    'JSON mapping canonical item IDs to their statement-of-origin when an identity spans two statements.';

-- Seed built-in identities
INSERT INTO ref_std_identity_check
    (check_id, description, statement_type, lhs_item_id, rhs_item_ids, rhs_signs, tolerance_bp, cross_statement, sector_scope)
VALUES
    (
        'accounting_equation',
        'total_assets = total_liabilities + total_equity',
        'balance_sheet',
        'total_assets',
        ARRAY['total_liabilities', 'total_equity'],
        ARRAY[1, 1]::SMALLINT[],
        1,
        NULL,
        'universal'
    ),
    (
        'assets_current_plus_noncurrent',
        'total_assets = total_current_assets + total_noncurrent_assets',
        'balance_sheet',
        'total_assets',
        ARRAY['total_current_assets', 'total_noncurrent_assets'],
        ARRAY[1, 1]::SMALLINT[],
        1,
        NULL,
        'corp'
    ),
    (
        'liabilities_current_plus_noncurrent',
        'total_liabilities = total_current_liabilities + total_noncurrent_liabilities',
        'balance_sheet',
        'total_liabilities',
        ARRAY['total_current_liabilities', 'total_noncurrent_liabilities'],
        ARRAY[1, 1]::SMALLINT[],
        1,
        NULL,
        'corp'
    ),
    (
        'net_change_identity',
        'net_change_in_cash = cash_flow_from_operations + cash_flow_from_investing + cash_flow_from_financing + fx_effect_on_cash',
        'cash_flow_statement',
        'net_change_in_cash',
        ARRAY['cash_flow_from_operations', 'cash_flow_from_investing', 'cash_flow_from_financing', 'fx_effect_on_cash'],
        ARRAY[1, 1, 1, 1]::SMALLINT[],
        1,
        NULL,
        'universal'
    ),
    (
        'cash_bridge',
        'ending_cash_balance = beginning_cash_balance + net_change_in_cash',
        'cash_flow_statement',
        'ending_cash_balance',
        ARRAY['beginning_cash_balance', 'net_change_in_cash'],
        ARRAY[1, 1]::SMALLINT[],
        1,
        '{"ending_cash_balance": "balance_sheet/cash_and_cash_equivalents/current_period", "beginning_cash_balance": "balance_sheet/cash_and_cash_equivalents/prior_period"}'::JSONB,
        'universal'
    )
ON CONFLICT (check_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- ref_std_identity_violation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ref_std_identity_violation (
    violation_id    BIGSERIAL PRIMARY KEY,
    entity_id       TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    fiscal_year     SMALLINT NOT NULL,
    fiscal_period   TEXT NOT NULL,
    check_id        TEXT NOT NULL REFERENCES ref_std_identity_check (check_id),
    lhs_value       NUMERIC,
    rhs_value       NUMERIC,
    delta           NUMERIC,
    delta_bp        NUMERIC,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ref_std_identity_violation IS
    'Per-entity audit trail of accounting identity failures detected during the derivation pass.';

CREATE INDEX IF NOT EXISTS idx_rsiv_entity  ON ref_std_identity_violation (entity_id, fiscal_year, check_id);
CREATE INDEX IF NOT EXISTS idx_rsiv_check   ON ref_std_identity_violation (check_id, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_rsiv_recent  ON ref_std_identity_violation (detected_at DESC);
