-- Add market cap to per-ticker EPS revision snapshots, and rebuild the
-- aggregate view with a cap-weighted forward P/E.
--
-- Background: migration 061 switched the aggregate forward P/E to an
-- equal-weighted trimmed mean (~18.5 for the US broad universe). Equal-
-- weighting under-represents the mega-cap multiples that drive the SPX
-- forward P/E (~22). Index providers always cap-weight: SUM(pe * mcap) /
-- SUM(mcap). To do that we need market cap per ticker, captured at the
-- snapshot moment so each historical aggregate reflects the cap weights
-- of that day.
--
-- yfinance's Ticker.info already returns ``marketCap`` for free (same call
-- we make for forwardPE), so this is zero extra HTTP cost in the ingest.
-- Existing rows get market_cap = NULL; the next snapshot run repopulates.

SET search_path TO sec, public;

ALTER TABLE fact_earnings_revision
    ADD COLUMN IF NOT EXISTS market_cap NUMERIC(20, 0);

COMMENT ON COLUMN fact_earnings_revision.market_cap IS
    'Ticker market capitalisation in USD at snapshot_date, captured via '
    'yfinance Ticker.info["marketCap"]. NULL for legacy snapshots taken '
    'before migration 062.';

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
    -- Equal-weighted trimmed mean (legacy from migration 061). Kept as a
    -- fallback the frontend can read when market_cap coverage is poor.
    AVG(forward_pe) FILTER (
        WHERE ticker IS NOT NULL AND forward_pe BETWEEN 5 AND 100
    )                                                AS avg_forward_pe_eq,
    -- Cap-weighted forward P/E: index-style aggregation. Filters apply to
    -- BOTH numerator and denominator so a ticker only contributes if its
    -- (forward_pe, market_cap) pair is valid.
    SUM(forward_pe * market_cap) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe BETWEEN 5 AND 100
          AND market_cap > 0
    )
    / NULLIF(SUM(market_cap) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe BETWEEN 5 AND 100
          AND market_cap > 0
    ), 0)                                            AS avg_forward_pe,
    -- Median over positive fwd P/E (robust point estimate; unweighted).
    percentile_cont(0.5) WITHIN GROUP (ORDER BY forward_pe)
        FILTER (WHERE ticker IS NOT NULL AND forward_pe > 0)
                                                     AS median_forward_pe,
    -- Coverage diagnostics so callers can detect when cap-weight is unsafe.
    COUNT(*) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe BETWEEN 5 AND 100
          AND market_cap > 0
    )::int                                           AS n_cap_weighted,
    MAX(cycle_factor)                                AS cycle_factor
FROM fact_earnings_revision
WHERE ticker IS NOT NULL
GROUP BY jurisdiction, snapshot_date;

COMMENT ON VIEW v_earnings_revision_aggregate IS
    'Per-snapshot aggregate of fact_earnings_revision ticker rows. '
    'avg_forward_pe is CAP-WEIGHTED over (5 <= fwd_pe <= 100, mcap > 0). '
    'avg_forward_pe_eq is the legacy equal-weighted trimmed mean (kept as '
    'fallback). median_forward_pe is an unweighted robust point estimate. '
    'breadth = analysts_up / (analysts_up + analysts_down). n_cap_weighted '
    'reports how many tickers fed the cap-weighted average.';
