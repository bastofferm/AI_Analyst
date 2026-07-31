"""Postgres connection helper for the AI Analyst tools.

Self-contained so ai_analyst/ doesn't have to import ops_dashboard (which would
create a circular import once callbacks.py is registered from there).
"""
from __future__ import annotations

import warnings

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
import psycopg2
import psycopg2.extras
import pandas as pd

from xbrl_sec.sec.settings import load_settings

_SETTINGS = load_settings()


def get_conn():
    return psycopg2.connect(
        _SETTINGS.database_url,
        connect_timeout=10,
        options=f"-c search_path={_SETTINGS.schema},public -c statement_timeout=60000 -c default_transaction_read_only=on",
    )


def read_sql(sql: str, params: dict | list | None = None) -> pd.DataFrame:
    conn = get_conn()
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="pandas only supports SQLAlchemy connectable.*",
                category=UserWarning,
            )
            return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def fetchall_dict(sql: str, params: tuple | list | None = None) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or [])
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows
    finally:
        conn.close()
