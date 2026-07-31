-- Activate residual derivation for "other_*" catch-all profile rows.
--
-- assembly.py:_derive_residual computes other_x = parent - sum(known siblings)
-- using ref_std_item_edge rollup edges, but ONLY for profile rows whose
-- display_role = 'RESIDUAL'. The edge graph is fully wired (every other_*
-- item has parent + sibling edges), yet no profile row carries the RESIDUAL
-- role, so the derivation never fires.
--
-- This is the keystone of the two-layer dictionary architecture: roots +
-- direct children get mapped from XBRL; everything long-tail is absorbed
-- arithmetically into "other" lines instead of being chased with per-concept
-- mappings.
--
-- Safe: _derive_residual only fills years where the row has no value, so
-- entities whose facts already map to these items keep their reported values.

SET search_path TO sec, public;

UPDATE ref_std_statement_display_profile
SET display_role = 'RESIDUAL',
    updated_at = now()
WHERE line_item_id IN (
    -- balance sheet catch-alls
    'other_current_assets',
    'other_noncurrent_assets',
    'other_current_liabilities',
    'other_noncurrent_liabilities',
    -- cash flow catch-alls
    'other_operating_activities',
    'other_investing_activities',
    'other_financing_activities',
    -- income statement catch-all
    'other_operating_income_expense_net'
)
AND display_role <> 'RESIDUAL';
