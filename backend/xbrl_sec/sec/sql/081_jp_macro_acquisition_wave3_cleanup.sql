-- JP macro acquisition wave 3 cleanup: hide stale visible JP feeds.
-- Keep facts in place for historical research, but do not expose them on /macro.

SET search_path TO sec, public;

UPDATE ref_macro_series s
SET    is_active = FALSE,
       story_tile_slot = NULL,
       importance = 3
WHERE  s.series_id = 'OECD:CLI_JP'
AND    COALESCE(
           (SELECT MAX(f.date) FROM fact_macro f WHERE f.series_id = s.series_id),
           DATE '1900-01-01'
       ) < CURRENT_DATE - INTERVAL '18 months';
