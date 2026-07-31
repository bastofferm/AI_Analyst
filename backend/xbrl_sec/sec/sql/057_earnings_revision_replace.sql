-- 057_earnings_revision_replace.sql
--
-- Replaces the existing fact_earnings_revision table (which held an annual
-- realised-EPS YoY proxy written by earnings_revision.py) with a ticker-level
-- snapshot schema that holds yfinance NTM analyst-consensus data.
--
-- The aggregate view v_earnings_revision_aggregate preserves the shape the
-- /api/macro/earnings-revision endpoint expects (date, jurisdiction, breadth,
-- cycle_factor) so the frontend continues to work — it just returns an empty
-- result set until the yfinance snapshot pipeline has been run.
--
-- DESTRUCTIVE: drops the prior table. The realised-EPS proxy is deprecated;
-- earnings_revision.py is being decommissioned as part of this migration.

SET search_path TO sec, public;

-- Drop the prior realised-EPS proxy (schema was:
--   date, jurisdiction, breadth, cycle_factor, n_companies).
DROP VIEW  IF EXISTS v_earnings_revision_aggregate CASCADE;
DROP TABLE IF EXISTS fact_earnings_revision CASCADE;

CREATE TABLE fact_earnings_revision (
    jurisdiction   TEXT        NOT NULL,        -- 'US' (extend later)
    snapshot_date  DATE        NOT NULL,        -- date the snapshot was taken
    ticker         TEXT,                        -- NULL = aggregate row (e.g. ^GSPC)
    ticker_key     TEXT GENERATED ALWAYS AS (COALESCE(ticker, '')) STORED,
    analysts_up    INT,
    analysts_down  INT,
    analysts_flat  INT,
    forward_pe     NUMERIC(8,2),                -- per-ticker fwd P/E
    forward_eps    NUMERIC(10,4),
    breadth        NUMERIC(6,4),                -- aggregate rows only
    cycle_factor   NUMERIC(8,4),                -- joined from fact_macro_factor.us_cycle
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction, snapshot_date, ticker_key)
);

CREATE INDEX idx_earnings_revision_date
    ON fact_earnings_revision (jurisdiction, snapshot_date DESC);

CREATE INDEX idx_earnings_revision_ticker
    ON fact_earnings_revision (ticker, snapshot_date DESC)
    WHERE ticker IS NOT NULL;

COMMENT ON TABLE fact_earnings_revision IS
    'yfinance NTM EPS-revision snapshots. One row per (jurisdiction, snapshot_date, ticker). '
    'NULL ticker = aggregate (index-level) row. Aggregate breadth is computed by v_earnings_revision_aggregate.';

-- One row per (jurisdiction, snapshot_date): breadth + average forward P/E.
CREATE OR REPLACE VIEW v_earnings_revision_aggregate AS
SELECT
    jurisdiction,
    snapshot_date                          AS date,
    COUNT(*) FILTER (WHERE ticker IS NOT NULL)::int  AS n_tickers,
    SUM(analysts_up)                       AS analysts_up_total,
    SUM(analysts_down)                     AS analysts_down_total,
    CASE WHEN COALESCE(SUM(analysts_up), 0) + COALESCE(SUM(analysts_down), 0) > 0
         THEN SUM(analysts_up)::numeric / (SUM(analysts_up) + SUM(analysts_down))
         ELSE NULL END                     AS breadth,
    AVG(forward_pe) FILTER (WHERE ticker IS NOT NULL) AS avg_forward_pe,
    MAX(cycle_factor)                      AS cycle_factor
FROM fact_earnings_revision
WHERE ticker IS NOT NULL
GROUP BY jurisdiction, snapshot_date;

COMMENT ON VIEW v_earnings_revision_aggregate IS
    'Aggregate (index-wide) EPS revision breadth and avg forward P/E per snapshot day. '
    'Powers /api/macro/earnings-revision.';
