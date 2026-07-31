"""AQR factor data ingestion (Betting-Against-Beta, HML-Devil).

Adds two AQR-published factor series to the existing ``fact_fama_french``
table (PK is ``(dataset, factor, date)``) so they can be queried alongside
Ken French factors:

  - AQR:BAB_Developed_Daily         — Betting-Against-Beta (Low Vol proxy)
  - AQR:HML_Devil_Developed_Daily   — Dividend-Yield-tilted value factor

AQR distributes both as Excel workbooks with a multi-row header preamble.
The 'DEV' (Developed Markets) sheet is used for both. The first row with a
parseable date in column 0 marks the data block start.

The xlsx URLs occasionally change; users can override via env vars
``AQR_BAB_URL`` / ``AQR_HMLDEVIL_URL`` or supply a local file with
``--from-file``.

Run:
    python -m xbrl_sec.sec.sources.aqr_factors_ingest --full
    python -m xbrl_sec.sec.sources.aqr_factors_ingest --dataset bab --from-file ./bab.xlsx
"""
from __future__ import annotations

import argparse
import io
import os
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import market_run, mark_item_done, mark_item_running


AQR_BASE = "https://www.aqr.com/-/media/AQR/Documents/Insights/Data-Sets"

# AQR files have one sheet per factor with one column per country + a
# "Global" aggregate column (24 developed markets). We use Global as the
# "Developed" proxy. Sheet names verified against the live files 22-May-26.
DEFAULT_DATASETS: dict[str, dict[str, Any]] = {
    "AQR:BAB_Developed_Daily": {
        "url_env": "AQR_BAB_URL",
        "default_url": f"{AQR_BASE}/Betting-Against-Beta-Equity-Factors-Daily.xlsx",
        "sheet": "BAB Factors",
        "factor_col": "Global",
        "factor_name": "Low Vol",
    },
    "AQR:HML_Devil_Developed_Daily": {
        "url_env": "AQR_HMLDEVIL_URL",
        "default_url": f"{AQR_BASE}/The-Devil-in-HMLs-Details-Factors-Daily.xlsx",
        "sheet": "HML Devil",
        "factor_col": "Global",
        "factor_name": "Div. Yield",
    },
}


def _ssl_context():
    """Build an SSL context that trusts the certifi CA bundle.

    On Windows the Python ssl default trust store sometimes lacks AQR's CA
    chain. certifi ships a known-good bundle. Falls back to system trust
    if certifi is unavailable.
    """
    import ssl
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download_xlsx(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; mzqa-research/1.0)",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
        return resp.read()


def _find_header_row(df_preview) -> int:
    """Find first row index whose column 0 contains a parseable date.

    AQR sheets have variable-length narrative preambles before the data;
    the first cell of the header row is typically 'DATE' or similar, and
    the row immediately after holds the first observation.
    """
    import pandas as pd

    for i, cell in enumerate(df_preview.iloc[:, 0]):
        if pd.isna(cell):
            continue
        s = str(cell).strip().upper()
        if s in ("DATE", "DATES"):
            return i
    # Fallback: try to find first cell that parses as a date.
    for i, cell in enumerate(df_preview.iloc[:, 0]):
        try:
            pd.to_datetime(cell)
            return max(0, i - 1)  # one above the first datestamped row
        except (ValueError, TypeError):
            continue
    return 0


def _parse_aqr_excel(raw: bytes, sheet: str, factor_col: str) -> list[tuple[date, float]]:
    import pandas as pd

    # Two-pass read: first locate header row, then re-read with that header.
    preview = pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=None, nrows=40)
    header_idx = _find_header_row(preview)
    df = pd.read_excel(io.BytesIO(raw), sheet_name=sheet, header=header_idx)

    # Normalize column lookup (case-insensitive, strip whitespace).
    cols_norm = {str(c).strip().lower(): c for c in df.columns}
    date_col = next(
        (cols_norm[c] for c in cols_norm if c in ("date", "dates")),
        df.columns[0],
    )
    factor_lookup = cols_norm.get(factor_col.strip().lower())
    if factor_lookup is None:
        raise RuntimeError(
            f"AQR sheet '{sheet}' missing factor column '{factor_col}'. "
            f"Available: {list(df.columns)}"
        )

    df = df[[date_col, factor_lookup]].dropna()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])

    out: list[tuple[date, float]] = []
    for _, row in df.iterrows():
        try:
            d = row[date_col].date()
            v = float(row[factor_lookup])
        except (TypeError, ValueError):
            continue
        out.append((d, v))
    out.sort(key=lambda r: r[0])
    return out


def _upsert(dataset: str, factor: str, rows: list[tuple[date, float]]) -> None:
    if not rows:
        return
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_fama_french (dataset, factor, date, value)
            VALUES %s
            ON CONFLICT (dataset, factor, date) DO UPDATE SET
                value = EXCLUDED.value
            """,
            [(dataset, factor, d, v) for d, v in rows],
            page_size=2000,
        )


def _resolve_url(cfg: dict[str, Any]) -> str:
    return os.environ.get(cfg["url_env"], cfg["default_url"])


def fetch_aqr(
    datasets: list[str] | None = None,
    full: bool = False,
    from_file: Path | None = None,
) -> int:
    """Fetch & upsert AQR factor datasets. Returns rows written."""
    selected = datasets or list(DEFAULT_DATASETS.keys())
    total = 0

    with market_run("aqr", full, {"datasets": len(selected)}) as ctx:
        for dataset in selected:
            cfg = DEFAULT_DATASETS.get(dataset)
            if cfg is None:
                mark_item_done(
                    ctx, "aqr", dataset,
                    status="failed",
                    error=f"unknown dataset: {dataset}",
                )
                continue

            mark_item_running(ctx, "aqr", dataset)

            try:
                if from_file:
                    raw = from_file.read_bytes()
                else:
                    raw = _download_xlsx(_resolve_url(cfg))
                rows = _parse_aqr_excel(raw, cfg["sheet"], cfg["factor_col"])
            except Exception as exc:
                mark_item_done(
                    ctx, "aqr", dataset, status="failed", error=str(exc)[:4000]
                )
                continue

            if rows:
                _upsert(dataset, cfg["factor_name"], rows)

            mark_item_done(
                ctx,
                "aqr",
                dataset,
                status="succeeded" if rows else "skipped",
                rows_in=len(rows),
                rows_out=len(rows),
                min_date=min(d for d, _ in rows) if rows else None,
                max_date=max(d for d, _ in rows) if rows else None,
            )
            total += len(rows)
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest AQR factor datasets")
    p.add_argument("--full", action="store_true", help="Reload full history")
    p.add_argument(
        "--dataset",
        choices=["bab", "hml_devil", "all"],
        default="all",
        help="Which dataset to ingest",
    )
    p.add_argument("--from-file", type=Path, help="Use a local xlsx file instead of downloading")
    args = p.parse_args()

    mapping = {
        "bab": ["AQR:BAB_Developed_Daily"],
        "hml_devil": ["AQR:HML_Devil_Developed_Daily"],
        "all": list(DEFAULT_DATASETS.keys()),
    }
    n = fetch_aqr(datasets=mapping[args.dataset], full=args.full, from_file=args.from_file)
    print(f"aqr_factors_ingest: {n} rows")


if __name__ == "__main__":
    main()
