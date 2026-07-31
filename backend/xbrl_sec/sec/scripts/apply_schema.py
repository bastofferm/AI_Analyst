"""Apply SQL files in sec/sql to xbrl_sec.sec.

Each migration is executed in its own transaction so a failure in one file
does not roll back the others. Failures are logged but do not abort the
overall apply pass — that way the FastAPI lifespan can safely call this on
every startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from xbrl_sec.sec.db.connection import connect


_logger = logging.getLogger("xbrl_sec.apply_schema")


def apply_schema(sql_dir: Path | None = None) -> dict[str, list[str]]:
    """Apply every *.sql file under sql_dir in lexical order.

    Returns a summary of {"applied": [...], "failed": [(name, error), ...]}
    so callers can decide whether to surface failures to the user.
    """
    root = Path(__file__).resolve().parents[1]
    sql_dir = sql_dir or root / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No SQL files found in {sql_dir}")

    applied: list[str] = []
    failed: list[str] = []
    for path in files:
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8-sig"))
            applied.append(path.name)
            print(f"applied {path.name}", flush=True)
        except Exception as exc:  # noqa: BLE001 - continue with the next file
            failed.append(path.name)
            message = str(exc).strip().splitlines()[0][:200]
            _logger.warning("migration %s failed: %s", path.name, message)
            print(f"FAILED {path.name}: {message}", flush=True)
    return {"applied": applied, "failed": failed}


if __name__ == "__main__":
    result = apply_schema()
    print(f"\nSummary: applied={len(result['applied'])} failed={len(result['failed'])}")
    if result["failed"]:
        for name in result["failed"]:
            print(f"  - {name}")
