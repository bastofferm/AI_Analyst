-- Relax the cap-weighted forward P/E filter.
--
-- Migration 062 filtered to forward_pe BETWEEN 5 AND 100 for both the
-- equal-weighted and cap-weighted aggregates. The 5-floor was inherited from
-- the equal-weighted version where it suppressed small caps that report
-- structurally low forward P/E (deep-value, distressed, or thinly-followed
-- names that yfinance reports at e.g. 1.2x). Under cap-weighting those names
-- carry negligible weight, so the floor over-excludes legitimate
-- observations. Switching the cap-weighted aggregate to ``forward_pe > 0
-- AND forward_pe <= 100`` matches the median's positivity logic.
--
-- The equal-weighted (avg_forward_pe_eq) keeps the 5-floor as a fallback for
-- snapshots where market_cap coverage is too thin to cap-weight.
-- The median is also tightened to <= 100 for parity.

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
    -- Equal-weighted trimmed mean: keeps the 5-floor because without
    -- cap-weighting, the small-cap low-P/E tail drags the mean down.
    AVG(forward_pe) FILTER (
        WHERE ticker IS NOT NULL AND forward_pe BETWEEN 5 AND 100
    )                                                AS avg_forward_pe_eq,
    -- Cap-weighted forward P/E: any positive fwd P/E capped at 100.
    -- Cap-weighting naturally down-weights the small-cap low-P/E noise, so
    -- a 5-floor is unnecessary and over-excludes valid observations.
    SUM(forward_pe * market_cap) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe > 0
          AND forward_pe <= 100
          AND market_cap > 0
    )
    / NULLIF(SUM(market_cap) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe > 0
          AND forward_pe <= 100
          AND market_cap > 0
    ), 0)                                            AS avg_forward_pe,
    -- Median over the same population (positive, <= 100), unweighted.
    percentile_cont(0.5) WITHIN GROUP (ORDER BY forward_pe)
        FILTER (
            WHERE ticker IS NOT NULL
              AND forward_pe > 0
              AND forward_pe <= 100
        )                                            AS median_forward_pe,
    -- Coverage diagnostic for the cap-weighted average.
    COUNT(*) FILTER (
        WHERE ticker IS NOT NULL
          AND forward_pe > 0
          AND forward_pe <= 100
          AND market_cap > 0
    )::int                                           AS n_cap_weighted,
    MAX(cycle_factor)                                AS cycle_factor
FROM fact_earnings_revision
WHERE ticker IS NOT NULL
GROUP BY jurisdiction, snapshot_date;

COMMENT ON VIEW v_earnings_revision_aggregate IS
    'Per-snapshot aggregate of fact_earnings_revision ticker rows. '
    'avg_forward_pe is CAP-WEIGHTED over (0 < fwd_pe <= 100, mcap > 0). '
    'avg_forward_pe_eq is the equal-weighted trimmed mean (5..100) used as '
    'fallback when n_cap_weighted < 50. median_forward_pe is unweighted, '
    'over the same (0 < fwd_pe <= 100) population. breadth = analysts_up / '
    '(analysts_up + analysts_down).';
