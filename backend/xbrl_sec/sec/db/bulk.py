"""Bulk database helpers."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
import site

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
site.addsitedir(site.getusersitepackages())
import psycopg2.extras


def execute_values(cur, sql: str, rows: Iterable[Sequence], page_size: int = 1000) -> int:
    data = list(rows)
    if not data:
        return 0
    psycopg2.extras.execute_values(cur, sql, data, page_size=page_size)
    return len(data)
