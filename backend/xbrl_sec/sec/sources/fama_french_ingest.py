"""Full Ken French data library ingestion.

The public Ken French library is a catalogue of CSV zip files, not a fixed
set of FF3/FF5 factor files. This module discovers the catalogue, parses every
numeric table in every CSV, and stores the result in long format.
"""
from __future__ import annotations

import io
import csv
import hashlib
import math
import os
import re
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path
from html.parser import HTMLParser
from typing import Any

import numpy as np
import pandas as pd

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.market_runs import (
    market_run,
    mark_item_done,
    mark_item_running,
    previous_item_hash,
)

FF_BASE_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french"
INDEX_DATE = "2000-01-01"
PROJECT_ROOT = Path(os.environ.get("MZQA_ROOT", r"C:\Users\Bastian Offermann\Desktop\MZQA"))
TMP_DIR = PROJECT_ROOT / "market_data" / "tmp"
PSQL_EXE = os.environ.get("PSQL_EXE", r"C:\Program Files\PostgreSQL\18\bin\psql.exe")
DATABASE_URL = os.environ.get("XBRL_SEC_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/xbrl_sec")
ESSENTIAL_DATASETS = {
    "F-F_Research_Data_Factors_daily",
    "F-F_Research_Data_5_Factors_2x3_daily",
    "F-F_Momentum_Factor_daily",
    "Japan_3_Factors_Daily",
    "Japan_5_Factors_Daily",
    "Japan_Mom_Factor_Daily",
    "Developed_3_Factors_Daily",
    "Developed_5_Factors_Daily",
    "Developed_Mom_Factor_Daily",
    "Emerging_Markets_3_Factors_Daily",
    "Emerging_Markets_5_Factors_Daily",
    "Emerging_MOM_Factor",
}


class _FrenchLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.datasets: list[tuple[str, str, str]] = []
        self._in_link = False
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href") or ""
        if re.search(r"ftp/.*CSV\.zip", href, re.IGNORECASE):
            self._in_link = True
            self._href = href
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_link:
            return
        href = self._href or ""
        match = re.search(r"ftp/([^/]+CSV\.zip)", href, re.IGNORECASE)
        if match:
            self.datasets.append((match.group(1), " ".join(self._text).strip(), href))
        self._in_link = False
        self._href = None
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            text = data.strip()
            if text:
                self._text.append(text)


def _infer_frequency(filename: str) -> str:
    lower = filename.lower()
    if "daily" in lower:
        return "daily"
    if "weekly" in lower:
        return "weekly"
    return "monthly"


def _infer_region(filename: str) -> str:
    lower = filename.lower()
    if any(
        token in lower
        for token in (
            "international",
            "developed",
            "emerging",
            "asia",
            "europe",
            "north_america",
            "japan",
            "global",
            "pacific",
            "world",
        )
    ):
        return "International"
    return "US"


def _dataset_id(zip_name: str) -> str:
    return zip_name.replace("_CSV.zip", "").replace(".zip", "")


def _download_text(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MZQA"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("latin-1", errors="replace")


def _download_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 MZQA"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _scrape_dataset_list() -> list[tuple[str, str, str, str, str]]:
    html = _download_text(f"{FF_BASE_URL}/data_library.html", timeout=45)
    parser = _FrenchLinkParser()
    parser.feed(html)

    seen: set[str] = set()
    datasets: list[tuple[str, str, str, str, str]] = []
    for zip_name, link_text, href in parser.datasets:
        if zip_name in seen:
            continue
        seen.add(zip_name)
        dataset = _dataset_id(zip_name)
        url = href if href.startswith("http") else f"{FF_BASE_URL}/{href.lstrip('/')}"
        datasets.append(
            (
                dataset,
                link_text or dataset,
                url,
                _infer_frequency(zip_name),
                _infer_region(zip_name),
            )
        )
    return datasets


def _is_numeric(value: str) -> bool:
    try:
        float(value.strip())
        return True
    except Exception:
        return False


def _looks_like_date(value: str) -> bool:
    value = value.strip()
    return value.isdigit() and len(value) in (4, 6, 8)


def _parse_ff_date(value: str) -> pd.Timestamp | None:
    value = value.strip()
    try:
        if len(value) == 8:
            return pd.Timestamp(year=int(value[:4]), month=int(value[4:6]), day=int(value[6:8]))
        if len(value) == 6:
            return pd.Timestamp(year=int(value[:4]), month=int(value[4:6]), day=1)
        if len(value) == 4:
            return pd.Timestamp(year=int(value), month=1, day=1)
    except ValueError:
        return None
    return None


def _is_header_parts(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    nonempty = [part for part in parts if part.strip()]
    if not nonempty:
        return False
    if all(not _is_numeric(part) for part in nonempty):
        return True
    return parts[0].strip() == "" and all(_is_numeric(part) for part in nonempty)


def _next_nonempty(lines: list[str], start: int) -> tuple[int, int]:
    index = start
    blanks = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
        blanks += 1
    return index, blanks


def _short_label(label: str) -> str:
    return re.sub(r"\s+", "_", label.strip())[:35]


def _read_data_block(lines: list[str], start: int, col_names: list[str]) -> tuple[list[dict[str, Any]], int]:
    index = start
    while index < len(lines) and not lines[index].strip():
        index += 1

    rows: list[dict[str, Any]] = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        parts = [part.strip() for part in line.split(",")]
        if not parts or not _looks_like_date(parts[0]):
            break
        parsed_date = _parse_ff_date(parts[0])
        if parsed_date is None:
            index += 1
            continue
        row: dict[str, Any] = {"_date": parsed_date}
        for column, raw_value in zip(col_names, parts[1:]):
            try:
                numeric_value = float(raw_value)
            except Exception:
                continue
            if numeric_value <= -99.0:
                numeric_value = float("nan")
            row[column] = numeric_value
        if len(row) > 1:
            rows.append(row)
        index += 1
    return rows, index


def _parse_ff_csv(raw_text: str) -> list[pd.DataFrame]:
    lines = raw_text.splitlines()
    results: list[pd.DataFrame] = []
    last_col_names: list[str] | None = None
    pending_label: str | None = None
    found_first_data = False

    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue

        parts = [part.strip() for part in lines[index].split(",")]

        if _is_header_parts(parts):
            next_index, _ = _next_nonempty(lines, index + 1)
            data_follows = (
                next_index < len(lines)
                and _looks_like_date([part.strip() for part in lines[next_index].split(",")][0])
            )
            if data_follows:
                raw_cols = parts[1:] if parts[0].strip() == "" else parts
                raw_cols = [column or f"col_{pos}" for pos, column in enumerate(raw_cols)]
                if pending_label and found_first_data:
                    prefix = f"{_short_label(pending_label)}|"
                    col_names = [f"{prefix}{column}" for column in raw_cols]
                else:
                    col_names = raw_cols
                pending_label = None
                last_col_names = col_names
                records, next_index = _read_data_block(lines, index + 1, col_names)
                if records:
                    found_first_data = True
                    frame = pd.DataFrame(records).set_index("_date").dropna(how="all")
                    frame.index.name = None
                    if not frame.empty:
                        results.append(frame)
                index = next_index
                continue
            pending_label = None

        if not _looks_like_date(parts[0]):
            next_index, blanks = _next_nonempty(lines, index + 1)
            if next_index >= len(lines):
                index += 1
                continue

            next_parts = [part.strip() for part in lines[next_index].split(",")]
            next_is_header = _is_header_parts(next_parts)
            next_is_data = _looks_like_date(next_parts[0])

            if next_is_header and blanks <= 1 and found_first_data:
                pending_label = stripped
                index += 1
                continue

            if next_is_data and last_col_names is not None and blanks <= 1 and found_first_data:
                prefix = f"{_short_label(stripped)}|"
                col_names = [f"{prefix}{column.split('|', 1)[-1]}" for column in last_col_names]
                last_col_names = col_names
                pending_label = None
                records, next_index = _read_data_block(lines, index + 1, col_names)
                if records:
                    frame = pd.DataFrame(records).set_index("_date").dropna(how="all")
                    frame.index.name = None
                    if not frame.empty:
                        results.append(frame)
                index = next_index
                continue

            pending_label = None

        index += 1

    return results


def _download_and_parse(zip_url: str) -> tuple[list[pd.DataFrame], str]:
    payload = _download_bytes(zip_url)
    payload_hash = hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_name = next((name for name in archive.namelist() if name.lower().endswith(".csv")), None)
        if csv_name is None:
            raise RuntimeError("No CSV found in zip")
        raw_text = archive.read(csv_name).decode("latin-1", errors="replace")
    return _parse_ff_csv(raw_text), payload_hash


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except Exception:
        return None
    return result if math.isfinite(result) else None


def _levels(series: pd.Series) -> pd.Series:
    returns_decimal = series / 100.0
    log_returns = np.log(1.0 + returns_decimal.clip(lower=-0.9999))
    cumulative = log_returns.cumsum()
    base_date = pd.Timestamp(INDEX_DATE)
    previous = cumulative.index[cumulative.index <= base_date]
    base = cumulative.loc[previous[-1]] if len(previous) else 0.0
    return 100.0 * np.exp((cumulative - base).clip(lower=-700.0, upper=700.0))


def _psql(sql: str, timeout: int = 3600) -> str:
    completed = subprocess.run(
        [PSQL_EXE, DATABASE_URL, "-v", "ON_ERROR_STOP=1", "-X", "-q"],
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout


def _copy_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _latest_dataset_dates() -> dict[str, pd.Timestamp]:
    latest: dict[str, pd.Timestamp] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset, MAX(date)
            FROM fact_fama_french
            GROUP BY dataset
            """
        )
        for dataset, max_date in cur.fetchall():
            if dataset and max_date:
                latest[dataset] = pd.Timestamp(max_date)
    return latest


def _register_datasets(datasets: list[tuple[str, str, str, str, str]]) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = TMP_DIR / "ff_datasets_stage.csv"
    rows = [
        (dataset, description, frequency, region, dataset in ESSENTIAL_DATASETS, url)
        for dataset, description, url, frequency, region in datasets
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    _psql(
        f"""
        SET search_path TO sec, public;
        CREATE TEMP TABLE ff_dataset_stage (
            dataset text,
            description text,
            frequency text,
            region text,
            is_essential boolean,
            source_url text
        );
        \\copy ff_dataset_stage FROM '{_copy_path(csv_path)}' WITH (FORMAT csv)
        INSERT INTO dim_ff_dataset
            (dataset, description, frequency, region, is_essential, source_url)
        SELECT dataset, description, frequency, region, is_essential, source_url
        FROM ff_dataset_stage
        ON CONFLICT (dataset) DO UPDATE SET
            description  = EXCLUDED.description,
            frequency    = EXCLUDED.frequency,
            region       = EXCLUDED.region,
            source_url   = EXCLUDED.source_url,
            updated_at   = now();
        """
    )


def _rows_for_dataset(
    dataset: str,
    frames: list[pd.DataFrame],
    cutoff: pd.Timestamp | None,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for frame in frames:
        frame = frame.sort_index()
        if cutoff is not None:
            frame = frame[frame.index > cutoff]
        if frame.empty:
            continue
        for factor in frame.columns:
            series = frame[factor].dropna()
            if series.empty:
                continue
            try:
                log_returns = np.log(1.0 + (series / 100.0).clip(lower=-0.9999))
                levels = _levels(series)
            except Exception:
                log_returns = pd.Series(dtype=float)
                levels = pd.Series(dtype=float)

            for observation_date, return_pct in series.items():
                pct = _safe_float(return_pct)
                if pct is None:
                    continue
                value = pct / 100.0
                rows.append(
                    (
                        observation_date.date(),
                        str(factor),
                        value,
                        dataset,
                        pct,
                        _safe_float(log_returns.get(observation_date)) if not log_returns.empty else None,
                        _safe_float(levels.get(observation_date)) if not levels.empty else None,
                    )
                )
    return rows


def _stage_rows(run_id: str, dataset: str, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    stage_values = [(run_id, "fama_french", dataset, *row) for row in rows]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM stage_fama_french
            WHERE run_id = %s::uuid
              AND source_key = %s
            """,
            (run_id, dataset),
        )
        execute_values(
            cur,
            """
            INSERT INTO stage_fama_french
                (run_id, source, source_key, date, factor, value, dataset, return_pct, return_log, level)
            VALUES %s
            """,
            stage_values,
            page_size=10000,
        )


def _merge_dataset(run_id: str, dataset: str, *, replace_dataset: bool = False) -> int:
    safe_dataset = dataset.replace("'", "''")
    if replace_dataset:
        write_sql = f"""
        DELETE FROM fact_fama_french
            WHERE dataset = '{safe_dataset}';

        INSERT INTO fact_fama_french
            (date, factor, value, dataset, return_pct, return_log, level)
        SELECT DISTINCT ON (dataset, factor, date)
               date, factor, value, dataset, return_pct, return_log, level
        FROM stage_fama_french
        WHERE run_id = '{run_id}'::uuid
          AND source_key = '{safe_dataset}'
        ORDER BY dataset, factor, date, loaded_at DESC;
        """
    else:
        write_sql = """
        INSERT INTO fact_fama_french
            (date, factor, value, dataset, return_pct, return_log, level)
        SELECT DISTINCT ON (dataset, factor, date)
               date, factor, value, dataset, return_pct, return_log, level
        FROM stage_fama_french
        WHERE run_id = '{run_id}'::uuid
          AND source_key = '{safe_dataset}'
        ORDER BY dataset, factor, date, loaded_at DESC
        ON CONFLICT (dataset, factor, date) DO UPDATE SET
            value      = EXCLUDED.value,
            return_pct = EXCLUDED.return_pct,
            return_log = EXCLUDED.return_log,
            level      = EXCLUDED.level,
            updated_at = now();
        """.replace("{run_id}", run_id).replace("{safe_dataset}", safe_dataset)
    _psql(f"SET search_path TO sec, public;\n{write_sql}", timeout=7200)
    output = _psql(
        f"""
        SET search_path TO sec, public;
        COPY (
            SELECT COUNT(*)::text
            FROM stage_fama_french
            WHERE run_id = '{run_id}'::uuid
              AND source_key = '{safe_dataset}'
        ) TO STDOUT;
        """,
        timeout=120,
    ).strip()
    return int(output or 0)


def _clear_stage_dataset(run_id: str, dataset: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM stage_fama_french
            WHERE run_id = %s::uuid
              AND source_key = %s
            """,
            (run_id, dataset),
        )


def _filter_datasets(
    datasets: list[tuple[str, str, str, str, str]],
    full_library: bool,
) -> list[tuple[str, str, str, str, str]]:
    if full_library:
        return datasets
    return [row for row in datasets if row[0] in ESSENTIAL_DATASETS]


def fetch_fama_french(full: bool = False, full_library: bool = False) -> int:
    """Discover and download the full available Ken French CSV catalogue."""
    datasets = _scrape_dataset_list()
    if not datasets:
        raise RuntimeError("No Ken French CSV datasets discovered")

    _register_datasets(datasets)
    datasets = _filter_datasets(datasets, full_library=full_library)
    latest = {} if full else _latest_dataset_dates()
    print(f"Fama-French: discovered {len(datasets):,} CSV datasets")
    print(
        "Fama-French: mode "
        f"{'full refresh' if full else 'incremental'}; "
        f"{'full library' if full_library else 'essential datasets only'}"
    )

    total_written = 0
    failures: list[tuple[str, str]] = []
    with market_run("fama_french", full, {"datasets": len(datasets)}) as ctx:
        run_id = str(ctx.run_id)
        for index, (dataset, _description, url, _frequency, _region) in enumerate(datasets, start=1):
            print(f"[{index:>3}/{len(datasets)}] fetching {dataset}")
            mark_item_running(ctx, "fama_french", dataset, source_url=url)
            try:
                cutoff = None if full else latest.get(dataset)
                prior_hash = None if full else previous_item_hash("fama_french", dataset)
                frames, payload_hash = _download_and_parse(url)
                if not full and prior_hash and prior_hash == payload_hash:
                    mark_item_done(
                        ctx,
                        "fama_french",
                        dataset,
                        status="skipped",
                        rows_in=0,
                        rows_out=0,
                        min_date=latest.get(dataset).date() if latest.get(dataset) is not None else None,
                        max_date=latest.get(dataset).date() if latest.get(dataset) is not None else None,
                        source_url=url,
                        source_hash=payload_hash,
                    )
                    print("    unchanged; skipped")
                    continue
                if not full and prior_hash and prior_hash != payload_hash:
                    cutoff = None
                rows = _rows_for_dataset(dataset, frames, cutoff)
                if rows:
                    _stage_rows(run_id, dataset, rows)
                    rows_out = _merge_dataset(run_id, dataset, replace_dataset=full)
                    _clear_stage_dataset(run_id, dataset)
                    min_date = min(row[0] for row in rows)
                    max_date = max(row[0] for row in rows)
                else:
                    rows_out = 0
                    min_date = max_date = latest.get(dataset).date() if latest.get(dataset) is not None else None
                mark_item_done(
                    ctx,
                    "fama_french",
                    dataset,
                    status="succeeded",
                    rows_in=len(rows),
                    rows_out=rows_out,
                    min_date=min_date,
                    max_date=max_date,
                    source_url=url,
                    source_hash=payload_hash,
                )
            except Exception as exc:
                failures.append((dataset, str(exc)))
                mark_item_done(ctx, "fama_french", dataset, status="failed", error=str(exc)[:4000], source_url=url)
                print(f"    ERROR {dataset}: {exc}")
                continue
            total_written += rows_out
            print(f"    {rows_out:,} rows staged/merged")
            time.sleep(0.05)

        if failures:
            print("Fama-French failures:")
            for dataset, message in failures:
                print(f"  {dataset}: {message}")
            raise RuntimeError(f"Fama-French completed with {len(failures)} dataset failures")

    return total_written


def cleanup_fama_french_storage(
    apply: bool = False,
    implied_retention_years: int | None = 3,
) -> dict[str, object]:
    """Reduce Fama-French/factor storage to production essentials.

    Uses table replacement for fact_fama_french so PostgreSQL returns disk space
    promptly instead of accumulating huge dead tuples from a bulk DELETE.
    """
    essential = sorted(ESSENTIAL_DATASETS)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE dim_ff_dataset
               SET is_essential = dataset = ANY(%s),
                   updated_at = now()
            """,
            (essential,),
        )
        cur.execute(
            """
            SELECT reltuples::bigint, pg_total_relation_size(oid)
            FROM pg_class
            WHERE oid = 'fact_fama_french'::regclass
            """
        )
        ff_rows_est, ff_bytes = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM fact_fama_french
            WHERE dataset = ANY(%s)
            """,
            (essential,),
        )
        essential_rows = cur.fetchone()[0]
        cur.execute(
            """
            SELECT reltuples::bigint, pg_total_relation_size(oid)
            FROM pg_class
            WHERE oid = 'stage_fama_french'::regclass
            """
        )
        stage_rows_est, stage_bytes = cur.fetchone()
        cur.execute(
            """
            SELECT reltuples::bigint, pg_total_relation_size(oid)
            FROM pg_class
            WHERE oid = 'fact_factor_implied_returns'::regclass
            """
        )
        implied_rows_est, implied_bytes = cur.fetchone()
        retained_implied_est = None
        cutoff_sql = None
        if implied_retention_years is not None:
            cutoff_sql = f"CURRENT_DATE - INTERVAL '{int(implied_retention_years)} years'"
            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM fact_factor_implied_returns
                WHERE date >= {cutoff_sql}
                """
            )
            retained_implied_est = cur.fetchone()[0]

        result: dict[str, object] = {
            "applied": apply,
            "essential_datasets": len(essential),
            "fact_fama_french_rows_est": int(ff_rows_est or 0),
            "fact_fama_french_essential_rows": int(essential_rows or 0),
            "fact_fama_french_bytes": int(ff_bytes or 0),
            "stage_fama_french_rows_est": int(stage_rows_est or 0),
            "stage_fama_french_bytes": int(stage_bytes or 0),
            "implied_rows_est": int(implied_rows_est or 0),
            "implied_retained_rows": int(retained_implied_est) if retained_implied_est is not None else None,
            "implied_bytes": int(implied_bytes or 0),
            "implied_retention_years": implied_retention_years,
        }
        if not apply:
            return result

        cur.execute("TRUNCATE stage_fama_french")
        conn.commit()

        cur.execute("DROP TABLE IF EXISTS fact_fama_french_keep")
        cur.execute("CREATE TABLE fact_fama_french_keep (LIKE fact_fama_french INCLUDING ALL)")
        cur.execute(
            """
            INSERT INTO fact_fama_french_keep
            SELECT *
            FROM fact_fama_french
            WHERE dataset = ANY(%s)
            ON CONFLICT (dataset, factor, date) DO UPDATE SET
                value = EXCLUDED.value,
                return_pct = EXCLUDED.return_pct,
                return_log = EXCLUDED.return_log,
                level = EXCLUDED.level,
                updated_at = EXCLUDED.updated_at
            """,
            (essential,),
        )
        cur.execute("DROP TABLE fact_fama_french")
        cur.execute("ALTER TABLE fact_fama_french_keep RENAME TO fact_fama_french")
        cur.execute(
            """
            COMMENT ON TABLE fact_fama_french IS
                'Production Ken French factor datasets in long format. Default load keeps essential factor-model inputs only.';
            """
        )
        conn.commit()

        if implied_retention_years is not None:
            cur.execute("DROP TABLE IF EXISTS fact_factor_implied_returns_keep")
            cur.execute("CREATE TABLE fact_factor_implied_returns_keep (LIKE fact_factor_implied_returns INCLUDING ALL)")
            cur.execute(
                f"""
                INSERT INTO fact_factor_implied_returns_keep
                SELECT *
                FROM fact_factor_implied_returns
                WHERE date >= {cutoff_sql}
                ON CONFLICT (jurisdiction, ticker, date, model) DO UPDATE SET
                    implied_return = EXCLUDED.implied_return,
                    window_end = EXCLUDED.window_end,
                    updated_at = EXCLUDED.updated_at
                """
            )
            cur.execute("DROP TABLE fact_factor_implied_returns")
            cur.execute("ALTER TABLE fact_factor_implied_returns_keep RENAME TO fact_factor_implied_returns")
            cur.execute(
                """
                COMMENT ON TABLE fact_factor_implied_returns IS
                    'Retention-limited factor-implied daily returns; reproducible from factor loadings and factor returns.';
                """
            )
    return result
