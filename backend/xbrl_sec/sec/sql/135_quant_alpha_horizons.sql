-- 135_quant_alpha_horizons.sql
-- Multi-horizon alpha models: a model is now (market / country) x (forward-return horizon:
-- forward_1m | forward_3m | forward_6m | forward_12m). Re-key the training ledger so all four
-- horizons per market/firm coexist instead of overwriting one another.

-- quant_alpha_model: PK (model_key) -> (model_key, label). `label` already exists and is NOT NULL.
ALTER TABLE quant_alpha_model DROP CONSTRAINT IF EXISTS quant_alpha_model_pkey;
ALTER TABLE quant_alpha_model ADD PRIMARY KEY (model_key, label);

-- quant_alpha_coverage: add `label`, PK (jurisdiction, ticker) -> (jurisdiction, ticker, label).
-- Existing rows are forward_1m (the only horizon trained before this).
ALTER TABLE quant_alpha_coverage ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT 'forward_1m';
ALTER TABLE quant_alpha_coverage DROP CONSTRAINT IF EXISTS quant_alpha_coverage_pkey;
ALTER TABLE quant_alpha_coverage ADD PRIMARY KEY (jurisdiction, ticker, label);
