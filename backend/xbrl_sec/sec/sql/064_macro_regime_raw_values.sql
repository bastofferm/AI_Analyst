-- Persist the raw growth and inflation values that feed each macro regime
-- z-score, so the frontend tooltip can show actual percentages alongside
-- the z-coordinates without re-querying the underlying series.
--
-- growth_unit / inflation_unit are short labels ("YoY %" / "QoQ % ann.") used
-- by the tooltip; persisted on each row rather than derived because the unit
-- depends on the per-jurisdiction series treatment in macro_regime_compute.

SET search_path TO sec, public;

ALTER TABLE fact_macro_regime
    ADD COLUMN IF NOT EXISTS growth_value     NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS inflation_value  NUMERIC(10, 4),
    ADD COLUMN IF NOT EXISTS growth_unit      TEXT,
    ADD COLUMN IF NOT EXISTS inflation_unit   TEXT;

COMMENT ON COLUMN fact_macro_regime.growth_value IS
    'Raw growth value for the quarter (the input to growth_z). Unit is in '
    'growth_unit (e.g. ''QoQ %% (ann.)'' for US level-derived, ''YoY %%'' for '
    'ECB rate series).';
COMMENT ON COLUMN fact_macro_regime.inflation_value IS
    'Raw inflation value for the quarter (the input to inflation_z). Unit '
    'is in inflation_unit.';
