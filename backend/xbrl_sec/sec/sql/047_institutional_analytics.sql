SET search_path TO sec, public;

CREATE TABLE IF NOT EXISTS fact_institutional_narrative (
    manager_cik TEXT NOT NULL,
    report_period DATE NOT NULL,
    input_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    narrative TEXT NOT NULL,
    analytics_packet JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manager_cik, report_period, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_institutional_narrative_manager_period
    ON fact_institutional_narrative (manager_cik, report_period DESC, created_at DESC);
