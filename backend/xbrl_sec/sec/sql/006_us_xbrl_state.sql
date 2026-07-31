-- US filing-source state for mandatory companyfacts + XBRL package joins.

SET search_path TO sec, public;

ALTER TABLE source_filing_state
    ADD COLUMN IF NOT EXISTS xbrl_package_path TEXT,
    ADD COLUMN IF NOT EXISTS xbrl_downloaded BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_extracted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_cal_extracted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_pre_extracted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_def_extracted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_lab_extracted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_source_filing_us_xbrl
    ON source_filing_state (jurisdiction, entity_id, xbrl_downloaded, xbrl_extracted)
    WHERE jurisdiction = 'US';
