"""Database connection helpers.

All SQL in this package should either qualify objects with sec. or use this
connection context, which sets search_path to sec, public.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
import site

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
import psycopg2
import psycopg2.extensions

from xbrl_sec.sec.settings import load_settings


@contextmanager
def connect() -> Iterator[psycopg2.extensions.connection]:
    settings = load_settings()
    schema = settings.schema
    if not schema.replace("_", "").isalnum():
        raise ValueError(f"Unsafe PostgreSQL schema name: {schema!r}")
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}, public")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
