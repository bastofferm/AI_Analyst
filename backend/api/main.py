"""FastAPI entrypoint for the standalone AI_Analyst Investment Committee app.

Trimmed from the MZQA Terminal API to just the routers the committee app needs:
GICS metadata + screener (universe resolution / AI filtering / value-sentiment agent) +
sector aggregates + the single-stock and group committee endpoints. Connects to the
existing (read-only) Postgres warehouse; schema migrations are always skipped here.
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .db import acquire, close_pool, init_pool
from .routers import (
    ai_committee,
    ai_committee_group,
    company,
    fx,
    kpis,
    llm_meta,
    meta,
    prices,
    quant,
    screener,
    screener_agent,
    sector,
)
from .settings import get_settings


logger = logging.getLogger("mzqa.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await init_pool()
    logger.info("DB pool initialized (schema=%s)", settings.db_schema)

    # Warm the qlib alpha cross-section in the background so the Quant desk and the
    # value-sentiment scanner are fast on first use (the cold path imports qlib and
    # reads the warehouse). Optional — never blocks or breaks startup.
    def _warm_quant() -> None:
        try:
            from api.quant import alpha_signal, qlib_backtest
            alpha_signal.prewarm(("US", "JP"))
            for j in ("US", "JP"):       # the walk-forward backtest is slow; warm both markets
                qlib_backtest.prewarm(j)
        except Exception:  # noqa: BLE001
            logger.warning("quant prewarm skipped", exc_info=True)
    threading.Thread(target=_warm_quant, name="quant-prewarm", daemon=True).start()

    try:
        yield
    finally:
        await close_pool()
        logger.info("DB pool closed")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI_Analyst Committee API",
        version="0.1.0",
        description="Standalone backend for the MZQA Investment Committee app.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    app.include_router(meta.router,               prefix="/api/meta",     tags=["meta"])
    app.include_router(sector.router,             prefix="/api/sector",   tags=["sector"])
    app.include_router(prices.router,             prefix="/api/prices",   tags=["prices"])
    app.include_router(kpis.router,               prefix="/api/kpis",     tags=["kpis"])
    app.include_router(company.router,            prefix="/api/company",  tags=["company"])
    app.include_router(fx.router,                 prefix="/api/fx",       tags=["fx"])
    app.include_router(screener.router,           prefix="/api/screener", tags=["screener"])
    app.include_router(screener_agent.router,     prefix="/api/screener", tags=["screener-agent"])
    app.include_router(ai_committee.router,       prefix="/api/ai",       tags=["ai-committee"])
    app.include_router(ai_committee_group.router, prefix="/api/ai",       tags=["ai-committee-group"])
    app.include_router(llm_meta.router,           prefix="/api/llm",      tags=["llm"])
    app.include_router(quant.router,              prefix="/api/quant",    tags=["quant"])

    _mount_logos(app)

    @app.get("/api/healthz", tags=["meta"])
    async def healthz() -> dict:
        s = get_settings()
        try:
            async with acquire() as conn:
                row = await conn.fetchrow("SELECT 1 AS ok")
            db_status = "connected" if row and row["ok"] == 1 else "error"
        except Exception as exc:
            logger.exception("healthz DB check failed")
            db_status = f"error: {exc.__class__.__name__}"
        return {"status": "ok", "db": db_status, "schema": s.db_schema}

    return app


# --------------------------------------------------------------------- logos
# Company logo images are a shared MZQA asset, not part of this repo: US files
# are named by zero-padded CIK (0000320193.png), JP files by EDINET code
# (E02144.png). Point MZQA_LOGO_DIRS at them (os.pathsep-separated) to override
# the sibling-checkout default.

def _logo_dirs() -> list[pathlib.Path]:
    configured = os.environ.get("MZQA_LOGO_DIRS", "").strip()
    if configured:
        return [pathlib.Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent   # …/AI_Analyst
    roots = [
        repo_root / "company_metadata",                 # if ever vendored in
        repo_root.parent / "MZQA" / "company_metadata", # the usual sibling checkout
        repo_root.parent / "MZQA-Equity-Terminal" / "backend" / "company_metadata",
    ]
    return [r / name for r in roots for name in ("logo_images", "logo_images_jp")]


def _mount_logos(app: FastAPI) -> None:
    dirs = [d for d in _logo_dirs() if d.exists()]
    if not dirs:
        logger.warning("No company-logo directories found; /logos will always 404. "
                       "Set MZQA_LOGO_DIRS to the logo_images folders.")

    @app.get("/logos/{logo_id:path}", include_in_schema=False)
    async def logo_asset(logo_id: str):
        # Basename only — never let a caller walk out of the logo directories.
        safe = pathlib.PurePosixPath(logo_id.replace("\\", "/")).name
        if not safe or safe in {".", ".."} or safe != logo_id.replace("\\", "/").split("/")[-1]:
            raise HTTPException(status_code=404, detail="Logo not found")
        candidates = [safe] if "." in safe else [f"{safe}.png", f"{safe}.PNG"]
        for directory in dirs:
            for candidate in candidates:
                path = directory / candidate
                if path.is_file():
                    # Immutable content keyed by CIK/EDINET — cache hard.
                    return FileResponse(str(path), headers={"Cache-Control": "public, max-age=86400"})
        raise HTTPException(status_code=404, detail="Logo not found")


app = create_app()
