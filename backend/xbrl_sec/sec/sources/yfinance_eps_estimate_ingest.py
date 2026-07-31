"""yfinance NTM EPS-estimate snapshot ingest.

For each ticker in the configured S&P 500-style universe, snapshot:
  - analyst revision counts (up / down) over the trailing 30 days
  - forward EPS (NTM)
  - forward P/E (NTM)

One row per (jurisdiction, snapshot_date, ticker) in
``fact_earnings_revision``. An aggregate row with ticker=NULL is also
written using yfinance's ``^GSPC`` index-level forward P/E when available.

Aggregate breadth and avg forward P/E are computed by the DB view
``v_earnings_revision_aggregate`` rather than precomputed here, so the
per-ticker detail remains queryable.

This module builds a forward-looking time series **going forward only** —
yfinance does not retain historical snapshots, so backfill before 'today'
is not possible without a paid data source.

Run:
    python -m xbrl_sec.sec.sources.yfinance_eps_estimate_ingest
    python -m xbrl_sec.sec.sources.yfinance_eps_estimate_ingest --tickers AAPL MSFT NVDA
"""
from __future__ import annotations

import argparse
import logging
import math
from datetime import date
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running

logger = logging.getLogger("mzqa.yfinance_eps")


# ---------------------------------------------------------------------------
# Universe selection
# ---------------------------------------------------------------------------

def _default_universe() -> list[str]:
    """Active US large-cap tickers from dim_company_us.

    No dedicated ref_sp500_constituent table yet; we approximate by selecting
    primary US tickers on Nasdaq/NYSE/NYSE Arca with a non-null GICS sector.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT primary_ticker
            FROM   dim_company_us
            WHERE  primary_ticker IS NOT NULL
              AND  mapping_sector IS NOT NULL
              AND  exchange IN ('Nasdaq', 'NYSE', 'NYSE Arca', 'NYSEAmerican')
            ORDER  BY primary_ticker
            """
        )
        return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# yfinance shape helpers — the eps_revisions DataFrame has had multiple
# layouts across yfinance versions. Handle both: per-period rows with
# 'upLast30days'/'downLast30days', or a wide layout with explicit columns.
# ---------------------------------------------------------------------------

def _extract_revisions(t: Any) -> tuple[int | None, int | None]:
    """Return (analysts_up_last_30d, analysts_down_last_30d) best-effort."""
    try:
        df = t.eps_revisions
    except Exception:
        return None, None
    if df is None or len(df) == 0:
        return None, None

    # Prefer the row labelled '0q' or '+1q' (next quarter) if present.
    candidates: list[str] = []
    try:
        idx_lower = [str(i).lower() for i in df.index]
        for key in ("+1q", "0q", "currentquarter", "nextquarter", "+1y", "0y"):
            if key in idx_lower:
                candidates.append(df.index[idx_lower.index(key)])
    except Exception:
        pass
    if not candidates and len(df) > 0:
        candidates = [df.index[0]]

    up_cols = [c for c in df.columns if str(c).lower() in (
        "uplast30days", "up_last_30_days", "up_last_30days", "uprevisionslast30days", "up"
    )]
    down_cols = [c for c in df.columns if str(c).lower() in (
        "downlast30days", "down_last_30_days", "down_last_30days", "downrevisionslast30days", "down"
    )]

    if not up_cols or not down_cols:
        return None, None

    row = df.loc[candidates[0]]
    try:
        up = int(row[up_cols[0]]) if row[up_cols[0]] is not None else None
    except (TypeError, ValueError):
        up = None
    try:
        down = int(row[down_cols[0]]) if row[down_cols[0]] is not None else None
    except (TypeError, ValueError):
        down = None
    return up, down


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cycle_factor_today(jurisdiction: str = "US") -> float | None:
    """Latest fact_macro_factor value for the US cycle (joined onto snapshot rows)."""
    factor_id = {"US": "us_cycle", "JP": "jp_cycle"}.get(jurisdiction)
    if factor_id is None:
        return None
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT value
            FROM   fact_macro_factor
            WHERE  factor_id = %s
            ORDER  BY date DESC
            LIMIT  1
            """,
            (factor_id,),
        )
        row = cur.fetchone()
    return _safe_float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Main snapshot
# ---------------------------------------------------------------------------

def snapshot(
    tickers: list[str] | None = None,
    snapshot_date: date | None = None,
    jurisdiction: str = "US",
) -> int:
    """Snapshot one ticker universe; returns row count written."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance not installed (pip install yfinance)") from exc

    universe = tickers or _default_universe()
    if not universe:
        logger.warning("empty universe; nothing to snapshot")
        return 0

    snap_d = snapshot_date or date.today()
    cycle_val = _cycle_factor_today(jurisdiction)

    rows: list[tuple[Any, ...]] = []
    n_with_data = 0

    with market_run("yfinance_eps", False, {"tickers": len(universe), "date": snap_d.isoformat()}) as ctx:
        for tkr in universe:
            mark_item_running(ctx, "yfinance_eps", tkr)
            try:
                yt = yf.Ticker(tkr)
                info: dict[str, Any] = {}
                try:
                    info = yt.info or {}
                except Exception:
                    info = {}

                up, down = _extract_revisions(yt)
                fwd_pe = _safe_float(info.get("forwardPE"))
                fwd_eps = _safe_float(info.get("forwardEps"))
                mcap = _safe_int(info.get("marketCap"))

                if (
                    up is None and down is None
                    and fwd_pe is None and fwd_eps is None
                    and mcap is None
                ):
                    mark_item_done(ctx, "yfinance_eps", tkr, status="skipped")
                    continue

                n_with_data += 1
                rows.append((
                    jurisdiction,                   # jurisdiction
                    snap_d,                          # snapshot_date
                    tkr,                             # ticker
                    up,                              # analysts_up
                    down,                            # analysts_down
                    None,                            # analysts_flat
                    fwd_pe,                          # forward_pe
                    fwd_eps,                         # forward_eps
                    None,                            # breadth (aggregate-only)
                    cycle_val,                       # cycle_factor (denormalised)
                    mcap,                            # market_cap (USD, for cap-weighting)
                ))
                mark_item_done(
                    ctx, "yfinance_eps", tkr,
                    status="succeeded",
                    rows_in=1,
                    rows_out=1,
                )
            except Exception as exc:
                mark_item_done(
                    ctx, "yfinance_eps", tkr,
                    status="failed",
                    error=str(exc)[:4000],
                )

        # Aggregate / index-level row (ticker=NULL).
        try:
            spx = yf.Ticker("^GSPC")
            spx_info = spx.info or {}
            spx_fwd_pe = _safe_float(spx_info.get("forwardPE"))
        except Exception:
            spx_fwd_pe = None

        # Aggregate breadth: % of universe with up > down.
        up_count = sum(1 for r in rows if (r[3] or 0) > (r[4] or 0))
        total_with_revisions = sum(1 for r in rows if (r[3] is not None and r[4] is not None))
        breadth = (up_count / total_with_revisions) if total_with_revisions else None

        rows.append((
            jurisdiction,
            snap_d,
            None,                                   # ticker NULL = aggregate
            None,                                   # analysts_up
            None,                                   # analysts_down
            None,                                   # analysts_flat
            spx_fwd_pe,                              # forward_pe (index-level)
            None,                                   # forward_eps
            breadth,                                 # breadth (computed)
            cycle_val,
            None,                                   # market_cap (not meaningful at the aggregate row)
        ))

    if rows:
        _upsert(rows)
    logger.info(
        "yfinance_eps snapshot %s: %d ticker rows + 1 aggregate, %d had data",
        snap_d, len(rows) - 1, n_with_data,
    )
    return len(rows)


def _upsert(rows: list[tuple[Any, ...]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_earnings_revision
                (jurisdiction, snapshot_date, ticker,
                 analysts_up, analysts_down, analysts_flat,
                 forward_pe, forward_eps, breadth, cycle_factor, market_cap)
            VALUES %s
            ON CONFLICT (jurisdiction, snapshot_date, ticker_key) DO UPDATE SET
                analysts_up   = EXCLUDED.analysts_up,
                analysts_down = EXCLUDED.analysts_down,
                analysts_flat = EXCLUDED.analysts_flat,
                forward_pe    = EXCLUDED.forward_pe,
                forward_eps   = EXCLUDED.forward_eps,
                breadth       = EXCLUDED.breadth,
                cycle_factor  = EXCLUDED.cycle_factor,
                market_cap    = EXCLUDED.market_cap,
                updated_at    = now()
            """,
            rows,
            page_size=500,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Snapshot yfinance NTM EPS revisions and forward P/E")
    p.add_argument("--tickers", nargs="*", help="Specific tickers (default: dim_company_us universe)")
    p.add_argument("--jurisdiction", default="US")
    p.add_argument("--date", help="Override snapshot date (YYYY-MM-DD); defaults to today")
    args = p.parse_args()

    snap_d = date.fromisoformat(args.date) if args.date else None
    n = snapshot(tickers=args.tickers, snapshot_date=snap_d, jurisdiction=args.jurisdiction)
    print(f"yfinance_eps_estimate_ingest: {n} rows")


if __name__ == "__main__":
    main()
