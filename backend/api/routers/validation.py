from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from xbrl_sec.sec.quality.validate import validate_jurisdiction


router = APIRouter()


@router.get("/{jurisdiction}")
async def get_validation(jurisdiction: Literal["US", "JP"]) -> dict[str, Any]:
    try:
        return await run_in_threadpool(validate_jurisdiction, jurisdiction)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Validation failed: {exc.__class__.__name__}: {exc}",
        ) from exc
