-- Shared filing intelligence state for ownership and narrative sidecar pipelines.

SET search_path TO sec, public;

ALTER TABLE pipeline_stage_run
    DROP CONSTRAINT IF EXISTS pipeline_stage_run_jurisdiction_check;

ALTER TABLE pipeline_stage_run
    ADD CONSTRAINT pipeline_stage_run_jurisdiction_check
    CHECK (jurisdiction IN ('GLOBAL','US','JP','US_13F','US_13DG','US_INSIDER','US_MDA'));

ALTER TABLE pipeline_entity_state
    DROP CONSTRAINT IF EXISTS pipeline_entity_state_jurisdiction_check;

ALTER TABLE pipeline_entity_state
    ADD CONSTRAINT pipeline_entity_state_jurisdiction_check
    CHECK (jurisdiction IN ('GLOBAL','US','JP','US_13F','US_13DG','US_INSIDER','US_MDA'));

CREATE TABLE IF NOT EXISTS source_external_file_state (
    source_group TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT,
    local_path TEXT,
    source_hash TEXT,
    downloaded BOOLEAN NOT NULL DEFAULT false,
    downloaded_at TIMESTAMPTZ,
    download_error TEXT,
    parsed BOOLEAN NOT NULL DEFAULT false,
    parsed_at TIMESTAMPTZ,
    parse_error TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_group, source_key)
);

CREATE INDEX IF NOT EXISTS idx_source_external_file_state_group_download
    ON source_external_file_state (source_group, downloaded, parsed);

CREATE TABLE IF NOT EXISTS fact_filing_pipeline_issue (
    issue_id BIGSERIAL PRIMARY KEY,
    source_group TEXT NOT NULL,
    source_key TEXT,
    jurisdiction TEXT,
    entity_id TEXT,
    ticker TEXT,
    filing_id TEXT,
    severity TEXT NOT NULL DEFAULT 'WARN'
        CHECK (severity IN ('INFO','WARN','ERROR')),
    issue_code TEXT NOT NULL,
    issue_message TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','RESOLVED','WAIVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fact_filing_pipeline_issue_group
    ON fact_filing_pipeline_issue (source_group, status, severity);

CREATE INDEX IF NOT EXISTS idx_fact_filing_pipeline_issue_entity
    ON fact_filing_pipeline_issue (jurisdiction, entity_id, ticker);

COMMENT ON TABLE source_external_file_state IS
    'Reusable local/remote file cache state for filing-intelligence sidecar pipelines such as 13F, 13D/G, insider trades, and MD&A extensions.';

COMMENT ON TABLE fact_filing_pipeline_issue IS
    'Non-destructive issue log for ownership/narrative filing pipelines. Does not touch fundamentals, metrics, or governed concept mappings.';
