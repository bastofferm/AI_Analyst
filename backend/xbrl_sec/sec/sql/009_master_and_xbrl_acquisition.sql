-- Explicit US XBRL acquisition states on the existing filing inventory.

SET search_path TO sec, public;

ALTER TABLE source_filing_state
    ADD COLUMN IF NOT EXISTS xbrl_download_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS xbrl_acquisition_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS xbrl_error TEXT,
    ADD COLUMN IF NOT EXISTS xbrl_last_attempted_at TIMESTAMPTZ;

UPDATE source_filing_state
SET xbrl_acquisition_status = CASE
        WHEN jurisdiction <> 'US' THEN xbrl_acquisition_status
        WHEN xbrl_extracted AND xbrl_cal_extracted AND xbrl_pre_extracted
             AND xbrl_def_extracted AND xbrl_lab_extracted THEN 'extracted_full'
        WHEN xbrl_extracted THEN 'extracted_partial'
        WHEN xbrl_downloaded THEN 'downloaded'
        WHEN xbrl_download_attempted THEN 'not_found_or_error'
        ELSE 'pending'
    END
WHERE jurisdiction = 'US'
  AND xbrl_acquisition_status = 'pending';

CREATE INDEX IF NOT EXISTS idx_source_filing_us_xbrl_status
    ON source_filing_state (jurisdiction, xbrl_acquisition_status, entity_id)
    WHERE jurisdiction = 'US';

-- Filing-level inventory remains in source_filing_state; do not duplicate it
-- in a separate dimension table.
