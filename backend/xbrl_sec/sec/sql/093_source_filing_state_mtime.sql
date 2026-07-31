-- Incremental filing index: track file mtime so sync_xbrl_index can skip
-- unchanged files without opening/hashing them. Existing rows have NULL
-- source_mtime; the next index pass will populate it for files it sees,
-- treating any NULL as "unknown → re-hash this once". After that pass,
-- subsequent runs short-circuit per-file on mtime equality.

SET search_path TO sec, public;

ALTER TABLE source_filing_state
    ADD COLUMN IF NOT EXISTS source_mtime DOUBLE PRECISION;

-- No index on source_mtime: lookups are by (jurisdiction, filing_id) primary
-- key, with mtime fetched alongside in a single row read. An index would
-- not help and would slow upserts.
