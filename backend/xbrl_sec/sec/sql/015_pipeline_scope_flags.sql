ALTER TABLE dim_company_us
    ADD COLUMN IF NOT EXISTS include_in_pipeline BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pipeline_sample_group TEXT;

ALTER TABLE dim_company_jp
    ADD COLUMN IF NOT EXISTS include_in_pipeline BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS pipeline_sample_group TEXT;

DO $$
BEGIN
    IF (SELECT COUNT(*) FROM dim_company_us WHERE include_in_pipeline) = 0 THEN
        UPDATE dim_company_us
           SET include_in_pipeline = true,
               pipeline_sample_group = COALESCE(pipeline_sample_group, 'pilot_50_us'),
               updated_at = now();
    END IF;

    IF (SELECT COUNT(*) FROM dim_company_jp WHERE include_in_pipeline) = 0 THEN
        UPDATE dim_company_jp
           SET include_in_pipeline = true,
               pipeline_sample_group = COALESCE(pipeline_sample_group, 'jp_147'),
               updated_at = now();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_dim_company_us_pipeline_scope
    ON dim_company_us (include_in_pipeline, pipeline_sample_group, cik);

CREATE INDEX IF NOT EXISTS idx_dim_company_jp_pipeline_scope
    ON dim_company_jp (include_in_pipeline, pipeline_sample_group, edinet_code);
