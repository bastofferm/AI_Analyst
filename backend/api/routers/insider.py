from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from ..db import acquire


router = APIRouter()


class InsiderTxn(BaseModel):
    filed_date: Optional[str] = None
    reporting_owner_name: Optional[str] = None
    officer_title: Optional[str] = None
    transaction_date: Optional[str] = None
    transaction_code: Optional[str] = None
    shares_amount: Optional[float] = None
    price_per_share: Optional[float] = None
    acquired_disposed_code: Optional[str] = None


class InsiderResponse(BaseModel):
    ticker: str
    transactions: list[InsiderTxn]


@router.get("/{ticker}", response_model=InsiderResponse)
async def get_insider(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query("US"),
    limit: int = Query(40, ge=1, le=200),
) -> InsiderResponse:
    if jurisdiction == "JP":
        return InsiderResponse(ticker=ticker, transactions=[])

    sql_resolve = "SELECT cik::text AS cik FROM dim_company_us WHERE primary_ticker=$1 LIMIT 1"
    sql = """
        SELECT  f.filing_date,
                f.reporting_owner_name,
                f.officer_title,
                t.transaction_date,
                t.transaction_code,
                t.shares_amount,
                t.price_per_share,
                t.acquired_disposed_code
        FROM    fact_insider_filing f
        JOIN    fact_insider_transaction_non_derivative t
                ON  t.accession_number = f.accession_number
        WHERE   f.cik = $1
        ORDER   BY t.transaction_date DESC NULLS LAST, f.filing_date DESC NULLS LAST
        LIMIT   $2
    """

    async with acquire() as conn:
        row = await conn.fetchrow(sql_resolve, ticker)
        if row is None or not row["cik"]:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")
        rows = await conn.fetch(sql, row["cik"], limit)

    txns = [
        InsiderTxn(
            filed_date=r["filing_date"].isoformat() if r["filing_date"] else None,
            reporting_owner_name=r["reporting_owner_name"],
            officer_title=r["officer_title"],
            transaction_date=r["transaction_date"].isoformat() if r["transaction_date"] else None,
            transaction_code=r["transaction_code"],
            shares_amount=float(r["shares_amount"]) if r["shares_amount"] is not None else None,
            price_per_share=float(r["price_per_share"]) if r["price_per_share"] is not None else None,
            acquired_disposed_code=r["acquired_disposed_code"],
        )
        for r in rows
    ]
    return InsiderResponse(ticker=ticker, transactions=txns)
