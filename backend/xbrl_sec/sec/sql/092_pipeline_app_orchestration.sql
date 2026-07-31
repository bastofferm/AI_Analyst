SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS pipeline_app_run (
    app_run_id      UUID PRIMARY KEY,
    label           TEXT,
    category        TEXT NOT NULL,
    command_key     TEXT NOT NULL,
    jurisdiction    TEXT,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    argv            JSONB NOT NULL DEFAULT '[]'::jsonb,
    status          TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled','unknown')),
    process_id      INTEGER,
    cwd             TEXT,
    stdout_path     TEXT,
    stderr_path     TEXT,
    exit_code       INTEGER,
    error_message   TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_app_run_status_started
    ON pipeline_app_run (status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_app_run_command_started
    ON pipeline_app_run (category, command_key, started_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_app_event (
    event_id        BIGSERIAL PRIMARY KEY,
    app_run_id      UUID NOT NULL REFERENCES pipeline_app_run(app_run_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    stage_run_id    UUID REFERENCES pipeline_stage_run(run_id),
    jurisdiction    TEXT,
    stage           TEXT,
    entity_id       TEXT,
    message         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_app_event_run_created
    ON pipeline_app_event (app_run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_app_event_stage
    ON pipeline_app_event (stage_run_id);

CREATE TABLE IF NOT EXISTS pipeline_scope_profile (
    profile_id      UUID PRIMARY KEY,
    jurisdiction    TEXT NOT NULL CHECK (jurisdiction IN ('US','JP')),
    name            TEXT NOT NULL,
    description     TEXT,
    entity_ids      TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    filters         JSONB NOT NULL DEFAULT '{}'::jsonb,
    sample_group    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction, name)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_scope_profile_jurisdiction
    ON pipeline_scope_profile (jurisdiction, name);

COMMENT ON TABLE pipeline_app_run IS
    'Runs launched by the standalone dataPipelineApp. Actual pipeline work still executes through xbrl_sec.sec.cli.';
COMMENT ON TABLE pipeline_app_event IS
    'App-level events and progress annotations for pipeline run visualization.';
COMMENT ON TABLE pipeline_scope_profile IS
    'Named entity scopes that can be applied to include_in_pipeline/pipeline_sample_group for repeatable incremental passes.';
