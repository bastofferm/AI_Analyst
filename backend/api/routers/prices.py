from __future__ import annotations

from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Path, Query

from ..db import acquire

router = APIRouter()


def _price_ticker(ticker: str, jurisdiction: str) -> str:
    if jurisdiction == "JP" and ticker.upper().endswith(".T"):
        return ticker[:-2]
    return ticker


@router.get("/{ticker}")
async def get_prices(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query("US"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
) -> dict:
    tbl = "fact_prices_us" if jurisdiction == "US" else "fact_prices_jp"
    stored_ticker = _price_ticker(ticker, jurisdiction)

    today = date.today()
    d_to = date_to or today
    d_from = date_from or (d_to - timedelta(days=5 * 366))

    sql = f"""
        SELECT date, COALESCE(adj_close, close) AS close
        FROM   {tbl}
        WHERE  ticker = $1
          AND  date BETWEEN $2 AND $3
          AND  COALESCE(adj_close, close) IS NOT NULL
        ORDER  BY date
    """

    async with acquire() as conn:
        rows = await conn.fetch(sql, stored_ticker, d_from, d_to)

    return {
        "ticker": ticker,
        "date_from": str(d_from),
        "date_to": str(d_to),
        "prices": [{"date": str(r["date"]), "close": float(r["close"])} for r in rows],
    }
