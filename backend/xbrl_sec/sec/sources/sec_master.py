"""US company master loading from copied SEC metadata."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings


def _metadata_path() -> Path:
    return load_settings().market_data_root / "us_sec" / "metadata" / "company_tickers.json"


def _normalize_cik(value: Any) -> str:
    return str(value).strip().zfill(10)


def load_company_tickers(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or _metadata_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict) and "fields" in payload and "data" in payload:
        fields = payload["fields"]
        for item in payload["data"]:
            rec = dict(zip(fields, item))
            rows.append(rec)
    elif isinstance(payload, dict):
        rows.extend(v for v in payload.values() if isinstance(v, dict))
    else:
        raise ValueError(f"Unsupported company_tickers.json format at {path}")
    return rows


def refresh_master(path: Path | None = None) -> int:
    raw = load_company_tickers(path)
    rows = []
    for rec in raw:
        cik = _normalize_cik(rec.get("cik") or rec.get("cik_str"))
        ticker = (rec.get("ticker") or rec.get("primary_ticker") or "").strip() or None
        name = (rec.get("title") or rec.get("name") or "").strip() or None
        exchange = (rec.get("exchange") or "").strip() or None
        if not cik or not ticker:
            continue
        rows.append((cik, name, ticker, exchange))
    sql = """
        INSERT INTO dim_company_us (cik, name, primary_ticker, exchange)
        VALUES %s
        ON CONFLICT (cik) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, dim_company_us.name),
            primary_ticker = COALESCE(EXCLUDED.primary_ticker, dim_company_us.primary_ticker),
            exchange = COALESCE(EXCLUDED.exchange, dim_company_us.exchange),
            updated_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows, page_size=2000)
