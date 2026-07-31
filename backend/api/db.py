from __future__ import annotations

import asyncpg
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from .settings import get_settings


_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    s = get_settings()

    _pool = await asyncpg.create_pool(
        dsn=s.database_url,
        min_size=s.pool_min,
        max_size=s.pool_max,
        command_timeout=60,
        server_settings={
            "search_path": s.db_schema,
            "statement_timeout": str(s.statement_timeout_ms),
            "application_name": "mzqa-api",
        },
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() in the FastAPI lifespan.")
    return _pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn
