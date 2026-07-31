"""FX rates for the frontend currency switcher.

`GET /api/fx` returns the latest spot rates from the macro warehouse
(fact_macro, FRED daily series) expressed as units of currency per 1 USD:

    { "as_of": "2026-06-18", "rates": { "USD": 1.0, "JPY": 161.37, "EUR": 0.8718 } }

Currencies whose series are missing are simply omitted — the frontend only
offers conversion targets it can actually compute. `as_of` is the oldest of
the contributing observation dates, so the caller can badge staleness honestly.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import acquire

router = APIRouter()

# series_id → (currency code, transform of "value" into units-per-USD).
# DEXJPUS is quoted JPY per USD (pass-through); DEXUSEU is USD per EUR (invert).
_FX_SERIES: dict[str, tuple[str, str]] = {
    "FRED:DEXJPUS": ("JPY", "direct"),
    "FRED:DEXUSEU": ("EUR", "invert"),
}


class FxResponse(BaseModel):
    as_of: str | None = None
    rates: dict[str, float]


@router.get("", response_model=FxResponse)
@router.get("/", response_model=FxResponse, include_in_schema=False)
async def get_fx() -> FxResponse:
    rates: dict[str, float] = {"USD": 1.0}
    dates: list[str] = []
    async with acquire() as conn:
        for series_id, (code, transform) in _FX_SERIES.items():
            row = await conn.fetchrow(
                """
                SELECT date, value::float8 AS value
                FROM   fact_macro
                WHERE  series_id = $1 AND value IS NOT NULL AND value <> 0
                ORDER  BY date DESC
                LIMIT  1
                """,
                series_id,
            )
            if not row:
                continue
            value = float(row["value"])
            rates[code] = (1.0 / value) if transform == "invert" else value
            dates.append(str(row["date"]))
    return FxResponse(as_of=min(dates) if dates else None, rates=rates)
