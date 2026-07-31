SET search_path TO sec, public;

ALTER TABLE fact_fama_french
    DROP CONSTRAINT IF EXISTS fact_fama_french_pkey;

ALTER TABLE fact_fama_french
    ADD CONSTRAINT fact_fama_french_pkey PRIMARY KEY (dataset, factor, date);

DROP INDEX IF EXISTS idx_fama_french_date;

CREATE INDEX IF NOT EXISTS idx_fama_french_date
    ON fact_fama_french (date DESC, dataset, factor);

CREATE INDEX IF NOT EXISTS idx_fama_french_dataset_factor
    ON fact_fama_french (dataset, factor, date DESC);
