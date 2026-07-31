-- Rebuild v_earnings_revision_aggregate with a trimmed-mean forward P/E.
--
-- Background: migration 057 created this view with a naive AVG(forward_pe).
-- After the first full SPX-universe yfinance snapshot loaded 4,491 tickers,
-- the simple mean collapsed to ~4.9 because ~1,170 loss-making firms reported
-- negative forward P/E and a handful of micro-caps reported extreme outliers
-- (|fpe| > 5000). The trimmed mean over 5..100 (the range an institutional
-- screener would treat as "valid") brings the aggregate back to ~18.5, which
-- is in the right ballpark for a broad US universe.
--
-- This migration is idempotent (DROP + CREATE). It also adds a median_forward_pe
-- column as a robust alternative the frontend can switch to if desired.
--
-- PostgreSQL's CREATE OR REPLACE VIEW cannot reorder/rename columns, hence the
-- explicit DROP.

SET search_path TO sec, public;

DROP VIEW IF EXISTS v_earnings_revision_aggregate;

CREATE VIEW v_earnings_revision_aggregate AS
SELECT
    jurisdiction,
    snapshot_date                                    AS date,
    COUNT(*) FILTER (WHERE ticker IS NOT NULL)::int  AS n_tickers,
    SUM(analysts_up)                                 AS analysts_up_total,
    SUM(analysts_down)                               AS analysts_down_total,
    CASE WHEN COALESCE(SUM(analysts_up), 0) + COALESCE(SUM(analysts_down), 0) > 0
         THEN SUM(analysts_up)::numeric
              / (SUM(analysts_up) + SUM(analysts_down))
         ELSE NULL END                               AS breadth,
    -- Trimmed mean: only treat forward P/E in [5, 100] as "valid".
    -- Excludes loss-making firms (negative fwd P/E) and extreme outliers.
    AVG(forward_pe) FILTER (
        WHERE ticker IS NOT NULL AND forward_pe BETWEEN 5 AND 100
    )                                                AS avg_forward_pe,
    -- Robust alternative: median over positive fwd P/E only.
    percentile_cont(0.5) WITHIN GROUP (ORDER BY forward_pe)
        FILTER (WHERE ticker IS NOT NULL AND forward_pe > 0) AS median_forward_pe,
    MAX(cycle_factor)                                AS cycle_factor
FROM fact_earnings_revision
WHERE ticker IS NOT NULL
GROUP BY jurisdiction, snapshot_date;

COMMENT ON VIEW v_earnings_revision_aggregate IS
    'Per-snapshot aggregate of fact_earnings_revision ticker rows. '
    'avg_forward_pe uses a 5..100 trimmed mean; median_forward_pe drops only '
    'negative values. breadth = analysts_up / (analysts_up + analysts_down).';
