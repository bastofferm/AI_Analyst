-- Make recon tables useful as metric audit/provenance layers.

SET search_path TO sec, public;

ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS metric_type TEXT;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS importance INTEGER;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS unit_type TEXT;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS fallback_applied BOOLEAN;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS input_values JSONB;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS source_line_items TEXT[];
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS source_concept_ids TEXT[];
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS source_filing_ids TEXT[];
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS raw_trace JSONB;
ALTER TABLE fact_metrics_recon_us ADD COLUMN IF NOT EXISTS trace_quality TEXT;

ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS metric_type TEXT;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS importance INTEGER;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS unit_type TEXT;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS fallback_applied BOOLEAN;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS input_values JSONB;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS source_line_items TEXT[];
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS source_concept_ids TEXT[];
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS source_filing_ids TEXT[];
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS raw_trace JSONB;
ALTER TABLE fact_metrics_recon_jp ADD COLUMN IF NOT EXISTS trace_quality TEXT;

CREATE INDEX IF NOT EXISTS idx_recon_us_trace_quality
    ON fact_metrics_recon_us (trace_quality);

CREATE INDEX IF NOT EXISTS idx_recon_jp_trace_quality
    ON fact_metrics_recon_jp (trace_quality);

CREATE INDEX IF NOT EXISTS idx_recon_us_source_filing_ids
    ON fact_metrics_recon_us USING gin (source_filing_ids);

CREATE INDEX IF NOT EXISTS idx_recon_jp_source_filing_ids
    ON fact_metrics_recon_jp USING gin (source_filing_ids);
