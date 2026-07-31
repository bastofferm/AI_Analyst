SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS dim_ff_dataset (
    dataset      TEXT PRIMARY KEY,
    description  TEXT,
    frequency    TEXT,
    region       TEXT,
    is_essential BOOLEAN NOT NULL DEFAULT false,
    source_url   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE fact_fama_french
    ADD COLUMN IF NOT EXISTS return_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_log DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS level DOUBLE PRECISION;

UPDATE fact_fama_french
SET return_pct = COALESCE(return_pct, value * 100.0),
    return_log = COALESCE(return_log, CASE WHEN value > -1.0 THEN LN(1.0 + value) END)
WHERE value IS NOT NULL
  AND (return_pct IS NULL OR return_log IS NULL);

ALTER TABLE fact_fama_french
    DROP CONSTRAINT IF EXISTS fact_fama_french_pkey;

ALTER TABLE fact_fama_french
    ADD CONSTRAINT fact_fama_french_pkey PRIMARY KEY (dataset, factor, date);

CREATE INDEX IF NOT EXISTS idx_dim_ff_dataset_frequency
    ON dim_ff_dataset (frequency, region);

CREATE INDEX IF NOT EXISTS idx_fama_french_dataset_factor_date
    ON fact_fama_french (dataset, factor, date DESC);

CREATE INDEX IF NOT EXISTS idx_fama_french_factor_date
    ON fact_fama_french (factor, date DESC);

COMMENT ON TABLE dim_ff_dataset IS
    'Ken French data library catalogue discovered from the public data_library.html page.';

COMMENT ON TABLE fact_fama_french IS
    'Full Ken French data library in long format. value is decimal return for compatibility; return_pct stores original FF percent values.';
