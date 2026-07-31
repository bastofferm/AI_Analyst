"""FX ingestion + ETF quote-currency capture for currency-correct factor models.

Two jobs:
  ingest_fx()        — pull daily USD-per-unit rates for the currencies our ETFs
                       trade in, into sec.fact_fx.
  capture_quote_ccy()— record each ETF price series' quote currency (yfinance
                       fast_info) into sec.dim_etf_profile.quote_ccy.
"""
from __future__ import annotations

import logging
import warnings

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore")

# Currencies our DE/AT-listed ETFs quote in. yfinance pair = "{CCY}USD=X" gives
# USD per 1 unit. USD is identity. GBp (pence) shares GBP's FX (returns are
# invariant to the pence/pound scale factor).
FX_CCYS = ["EUR", "GBP", "CHF", "JPY", "SEK", "NOK", "DKK", "CAD", "AUD", "HKD"]


def ingest_fx(period: str = "max") -> dict[str, int]:
    import yfinance as yf

    rows: list[tuple] = []
    for ccy in FX_CCYS:
        try:
            h = yf.Ticker(f"{ccy}USD=X").history(period=period, auto_adjust=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fx %s failed: %s", ccy, exc)
            continue
        if h is None or h.empty or "Close" not in h.columns:
            continue
        for idx, r in h.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            v = r["Close"]
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v == v and v > 0:  # not NaN
                rows.append((ccy, d, v))
    # USD identity row set: cover the full date span we saw.
    if rows:
        dates = sorted({d for _, d, _ in rows})
        rows.extend(("USD", d, 1.0) for d in dates)
    if not rows:
        return {"pairs": 0, "rows": 0}
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO sec.fact_fx (ccy, fx_date, usd_per_unit) VALUES %s
            ON CONFLICT (ccy, fx_date) DO UPDATE SET
                usd_per_unit=EXCLUDED.usd_per_unit, updated_at=NOW()
            """,
            rows,
        )
    return {"pairs": len({c for c, _, _ in rows}), "rows": len(rows)}


def capture_quote_ccy(limit: int | None = None, only_missing: bool = True) -> dict[str, int]:
    """Record each ETF's quote currency via yfinance fast_info."""
    import yfinance as yf

    where = "WHERE EXISTS (SELECT 1 FROM sec.fact_prices_etf f WHERE f.isin=d.isin)"
    if only_missing:
        where += (" AND NOT EXISTS (SELECT 1 FROM sec.dim_etf_profile p "
                  "WHERE p.isin=d.isin AND p.quote_ccy IS NOT NULL)")
    sql = f"SELECT d.isin FROM sec.dim_etf d {where} ORDER BY d.aum_eur DESC NULLS LAST"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        isins = [r[0] for r in cur.fetchall()]

    ok = miss = 0
    for isin in isins:
        ccy = None
        try:
            ccy = yf.Ticker(isin).fast_info.get("currency")
        except Exception:
            ccy = None
        if not ccy:
            miss += 1
            continue
        with connect() as conn, conn.cursor() as cur:
            # Upsert: profile row may not exist yet for this ISIN.
            cur.execute(
                """
                INSERT INTO sec.dim_etf_profile (isin, quote_ccy, profile_status)
                VALUES (%s, %s, 'pending')
                ON CONFLICT (isin) DO UPDATE SET quote_ccy=EXCLUDED.quote_ccy, updated_at=NOW()
                """,
                (isin, ccy.upper()[:3]),
            )
        ok += 1
    return {"requested": len(isins), "ok": ok, "missing": miss}
