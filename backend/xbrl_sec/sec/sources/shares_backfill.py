"""Forward-fill fact_prices_{us,jp}.shares_outstanding from XBRL fundamentals.

Each filing's `period_end` opens a validity window that runs until the next
filing's period_end (or forever if it's the latest). Every daily price row
in that window inherits the filing's shares_outstanding_diluted (US) or
shares_outstanding (JP) value.

US has an additional refinement pass: monthly buyback rows from
`fact_us_monthly_buybacks` step the share count down WITHIN each anchor
window, reflecting the per-month repurchases reported in Item 5 (10-K)
and Item 2(c) (10-Q). This gives monthly resolution vs the quarterly
step function the anchor alone provides.

Single SQL UPDATE per jurisdiction per pass — no Python loop, no LATERAL.
Idempotent: re-running just re-applies the same mapping.

CLI:
    python -m xbrl_sec.sec.sources.shares_backfill --jurisdiction US
    python -m xbrl_sec.sec.sources.shares_backfill --jurisdiction JP
    python -m xbrl_sec.sec.sources.shares_backfill --jurisdiction all
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from xbrl_sec.sec.db.connection import connect


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# US — anchor source: prefer cover-page `shares_outstanding`
#     (dei:EntityCommonStockSharesOutstanding / us-gaap:CommonStockSharesOutstanding,
#      a point-in-time count at period_end) and fall back to the weighted-avg
#      `shares_outstanding_diluted` for the ~5% of CIKs where only the diluted
#      concept is tagged.
#        keyed by cik
#        ↳ fact_prices_us joined on dim_company_us.primary_ticker
# ---------------------------------------------------------------------------

_BACKFILL_US = """
WITH filed AS (
    SELECT  c.primary_ticker                                       AS ticker,
            s.cik,
            s.period_end                                           AS period_end,
            s.value::bigint                                        AS shares,
            CASE s.line_item_id
                WHEN 'shares_outstanding'         THEN 1   -- cover-page point-in-time
                WHEN 'shares_outstanding_diluted' THEN 2   -- weighted-avg fallback
            END                                                    AS prio
    FROM    sec.fact_fundamentals_std_us s
    JOIN    sec.dim_company_us c ON c.cik = s.cik
    WHERE   s.line_item_id IN ('shares_outstanding','shares_outstanding_diluted')
      AND   s.value > 0
      AND   c.primary_ticker IS NOT NULL
),
best AS (
    SELECT  DISTINCT ON (ticker, period_end)
            ticker, cik, period_end, shares
    FROM    filed
    ORDER   BY ticker, period_end, prio
),
ranges AS (
    SELECT  ticker,
            period_end                                                                   AS valid_from,
            LEAD(period_end) OVER (PARTITION BY ticker ORDER BY period_end)              AS valid_to,
            shares
    FROM    best
)
UPDATE  sec.fact_prices_us p
   SET  shares_outstanding = r.shares
  FROM  ranges r
 WHERE  p.ticker = r.ticker
   AND  p.date  >= r.valid_from
   AND  (r.valid_to IS NULL OR p.date < r.valid_to)
"""


# ---------------------------------------------------------------------------
# JP — fact_fundamentals_std_jp(line_item_id='shares_outstanding')
#        keyed by edinet_code
#        ↳ dim_company_jp.primary_ticker carries the .T suffix; fact_prices_jp
#          stores the bare code (e.g. 1301 vs 1301.T) → strip in the JOIN.
# ---------------------------------------------------------------------------

_BACKFILL_JP = """
WITH filed AS (
    SELECT  REPLACE(c.primary_ticker, '.T', '')                    AS ticker,
            s.period_end                                           AS period_end,
            s.value::bigint                                        AS shares
    FROM    sec.fact_fundamentals_std_jp s
    JOIN    sec.dim_company_jp c ON c.edinet_code = s.edinet_code
    WHERE   s.line_item_id = 'shares_outstanding'
      AND   s.value > 0
      AND   c.primary_ticker IS NOT NULL
),
ranges AS (
    SELECT  ticker,
            period_end                                                                   AS valid_from,
            LEAD(period_end) OVER (PARTITION BY ticker ORDER BY period_end)              AS valid_to,
            shares
    FROM    filed
)
UPDATE  sec.fact_prices_jp p
   SET  shares_outstanding = r.shares
  FROM  ranges r
 WHERE  p.ticker = r.ticker
   AND  p.date  >= r.valid_from
   AND  (r.valid_to IS NULL OR p.date < r.valid_to)
"""


# ---------------------------------------------------------------------------
# US monthly buyback refinement — runs AFTER `_BACKFILL_US`.
#
# Within each XBRL filing window [anchor_date, next_anchor_date), price rows
# default to the anchor's shares value. This pass steps that value down at
# each monthly buyback's period_end:
#    shares_t = anchor_shares - cum_buybacks_since_anchor_t
# The next anchor's filing replaces the value at next_anchor_date so we don't
# need to clip — the boundary is naturally handled by Pass A's later anchor.
# ---------------------------------------------------------------------------

_BACKFILL_US_MONTHLY = """
WITH filed AS (
    SELECT  c.primary_ticker                                                AS ticker,
            c.cik,
            s.period_end                                                    AS period_end,
            s.value::bigint                                                 AS shares,
            CASE s.line_item_id
                WHEN 'shares_outstanding'         THEN 1
                WHEN 'shares_outstanding_diluted' THEN 2
            END                                                             AS prio
    FROM    sec.fact_fundamentals_std_us s
    JOIN    sec.dim_company_us c ON c.cik = s.cik
    WHERE   s.line_item_id IN ('shares_outstanding','shares_outstanding_diluted')
      AND   s.value > 0
      AND   c.primary_ticker IS NOT NULL
),
anchors AS (
    SELECT  ticker, cik, period_end AS anchor_date,
            shares                                                          AS anchor_shares,
            LEAD(period_end) OVER (PARTITION BY ticker ORDER BY period_end) AS next_anchor_date
    FROM (
        SELECT DISTINCT ON (ticker, period_end) ticker, cik, period_end, shares
        FROM   filed
        ORDER  BY ticker, period_end, prio
    ) anchor_picks
),
-- Pair each monthly-buyback row with the anchor window it falls in.
buybacks_in_window AS (
    SELECT  a.ticker, a.anchor_date, a.anchor_shares, a.next_anchor_date,
            b.period_end                                                    AS month_end,
            b.shares_purchased
    FROM    anchors a
    JOIN    sec.fact_us_monthly_buybacks b ON b.cik = a.cik
    WHERE   b.shares_purchased IS NOT NULL AND b.shares_purchased > 0
      AND   b.period_end >  a.anchor_date
      AND   (a.next_anchor_date IS NULL OR b.period_end <= a.next_anchor_date)
),
-- For each buyback row, compute the running shares state right after that
-- month finishes, plus the next month boundary within the same anchor window.
segments AS (
    SELECT  ticker, anchor_date, anchor_shares, next_anchor_date, month_end,
            anchor_shares - SUM(shares_purchased) OVER (
                PARTITION BY ticker, anchor_date
                ORDER BY month_end
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            )                                                               AS shares_after,
            LEAD(month_end) OVER (
                PARTITION BY ticker, anchor_date ORDER BY month_end
            )                                                               AS next_month_end
    FROM    buybacks_in_window
)
UPDATE  sec.fact_prices_us p
   SET  shares_outstanding = s.shares_after
  FROM  segments s
 WHERE  p.ticker = s.ticker
   AND  p.date >= s.month_end
   AND  s.shares_after > 0
   AND  p.date < COALESCE(s.next_month_end, s.next_anchor_date, DATE '9999-12-31')
"""


def backfill_us_monthly_buybacks(since_date: date | None = None) -> int:
    """Apply monthly buyback step-downs to fact_prices_us.shares_outstanding.

    Must be called AFTER the standard `backfill('US', ...)` so the anchor
    values are populated first. Re-running is idempotent because each
    segment's shares_after is deterministic.
    """
    sql = _BACKFILL_US_MONTHLY
    params: tuple = ()
    if since_date is not None:
        sql = sql + "\n   AND  p.date >= %s"
        params = (since_date,)
    with connect() as conn, conn.cursor() as cur:
        logger.info("[US] monthly buyback refinement UPDATE %s…",
                    f"(since {since_date}) " if since_date else "")
        cur.execute(sql, params)
        rows = cur.rowcount
    logger.info("[US] %d price rows refined with monthly buybacks", rows)
    return rows


def backfill(jurisdiction: str, since_date: date | None = None) -> int:
    """Apply the forward-fill UPDATE for one jurisdiction; return rows touched.

    If `since_date` is given, the UPDATE is narrowed to `p.date >= since_date`,
    skipping every already-populated historical row — used by the live ingest
    path to maintain the column on freshly downloaded prices without re-doing
    a multi-million-row UPDATE every night.
    """
    sql = {"US": _BACKFILL_US, "JP": _BACKFILL_JP}[jurisdiction]
    params: tuple = ()
    if since_date is not None:
        sql = sql + "\n   AND  p.date >= %s"
        params = (since_date,)
    with connect() as conn, conn.cursor() as cur:
        logger.info("[%s] running backfill UPDATE %s…",
                    jurisdiction,
                    f"(since {since_date}) " if since_date else "")
        cur.execute(sql, params)
        rows = cur.rowcount
    logger.info("[%s] %d price rows updated", jurisdiction, rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward-fill fact_prices_*.shares_outstanding.")
    parser.add_argument(
        "--jurisdiction",
        choices=["US", "JP", "all"],
        default="all",
        help="Which jurisdiction(s) to backfill (default: all).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    targets = ["US", "JP"] if args.jurisdiction == "all" else [args.jurisdiction]
    total = 0
    for j in targets:
        total += backfill(j)
        if j == "US":
            # Refine the anchor-based value with per-month buyback step-downs.
            total += backfill_us_monthly_buybacks()
    logger.info("done: %d price-row updates total", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
