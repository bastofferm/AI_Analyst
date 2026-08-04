"""Train every alpha model — US, JP, and one per INTL country — in parallel, CPU-maxed, and
record the result in the training/coverage ledger (`quant_alpha_model` + `quant_alpha_coverage`).

The alpha model is cross-sectional per market (US/JP) and per country for INTL (a single "INTL"
model would mix Germany, Korea, Brazil…; per-country cross-sections are far more coherent). This
is the batch entrypoint the (manual or Claude-scheduled) quarterly retraining runs:

    # from backend/, with the qlib venv:
    python -m api.quant.qlib_train_all --all                 # US + JP + every country >= min-firms
    python -m api.quant.qlib_train_all --due                 # only models past their quarterly next_due
    python -m api.quant.qlib_train_all --models US,INTL:DE    # an explicit subset
    python -m api.quant.qlib_train_all --all --min-firms 200 --start 2016-01-01

Each model trains in its own process; LightGBM threads are split across the pool so the box's
cores are saturated without oversubscription. Every model degrades independently — one country
failing never sinks the batch.
"""
from __future__ import annotations

import argparse
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("mzqa.quant.train_all")

_QUARTER_DAYS = 91
_DEFAULT_MIN_FIRMS = 150
_DEFAULT_LABEL = "forward_1m"
# Thin INTL countries have sparse monthly cross-sections (clustered fiscal-year filings), so
# the strict 30-names/month training gate starves them of distinct dates. A lower gate lets them
# train; US/JP keep 30. (Deeper Yahoo history — the acquisition — is the complementary fix.)
_INTL_MIN_NAMES = 10


# --------------------------------------------------------------------------- model set
def intl_countries(min_firms: int) -> list[str]:
    """ISO-2 codes with >= ``min_firms`` firms carrying FY/TTM metrics (trainable cross-section)."""
    from xbrl_sec.sec.db.connection import connect
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.country_code, COUNT(DISTINCT m.ticker) AS firms
            FROM   fact_metrics_intl m
            JOIN   dim_company_intl d ON d.primary_ticker = m.ticker
            WHERE  m.fiscal_period IN ('FY','TTM') AND m.value IS NOT NULL
              AND  d.country_code IS NOT NULL AND d.country_code <> ''
            GROUP  BY d.country_code
            HAVING COUNT(DISTINCT m.ticker) >= %s
            ORDER  BY firms DESC
            """,
            (min_firms,),
        )
        return [str(r[0]).upper() for r in cur.fetchall()]


def all_model_keys(min_firms: int) -> list[str]:
    return ["US", "JP", *(f"INTL:{cc}" for cc in intl_countries(min_firms))]


def _due_jobs(jobs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Subset of (model_key, label) jobs whose ledger next_due has passed (or never trained)."""
    from xbrl_sec.sec.db.connection import connect
    with connect() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute("SELECT model_key, label, next_due FROM quant_alpha_model")
        due_by = {(str(k), str(lbl)): d for k, lbl, d in cur.fetchall()}
    today = date.today()
    return [(mk, lbl) for (mk, lbl) in jobs
            if (due_by.get((mk, lbl)) is None or due_by[(mk, lbl)] is None or due_by[(mk, lbl)] <= today)]


# --------------------------------------------------------------------------- ledger DDL + upserts
def _ensure_tables(cur) -> None:
    """Create the ledger tables if migration 134 has not been applied (idempotent)."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_alpha_model (
            model_key TEXT NOT NULL, jurisdiction TEXT NOT NULL, country_code TEXT,
            label TEXT NOT NULL, version TEXT NOT NULL, trained_at TIMESTAMPTZ NOT NULL,
            train_start DATE, train_end DATE, rank_ic DOUBLE PRECISION, n_train_names INTEGER,
            coverage_count INTEGER, status TEXT NOT NULL DEFAULT 'trained', next_due DATE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), diagnostics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (model_key, label)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_alpha_coverage (
            jurisdiction TEXT NOT NULL, ticker TEXT NOT NULL, model_key TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'forward_1m', country_code TEXT, last_as_of DATE,
            expected_return DOUBLE PRECISION, covered BOOLEAN NOT NULL DEFAULT TRUE,
            last_trained_at TIMESTAMPTZ, next_due DATE, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (jurisdiction, ticker, label)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS quant_alpha_coverage_model_idx ON quant_alpha_coverage (model_key)")


def _split_key(model_key: str) -> tuple[str, str | None]:
    if model_key.upper().startswith("INTL:"):
        return "INTL", model_key.split(":", 1)[1].upper()
    return model_key.upper(), None


def _record(model_key: str, artifact: Any, cross) -> dict[str, Any]:
    """Upsert one model's registry row + per-firm coverage from its latest cross-section."""
    import json
    import math

    from xbrl_sec.sec.db.bulk import execute_values
    from xbrl_sec.sec.db.connection import connect

    def _fin(v: Any) -> Any:  # NaN/inf → None (thin test segments yield NaN IC; invalid JSON/float8)
        return v if (v is None or (isinstance(v, (int, float)) and math.isfinite(v))) else None

    jurisdiction, country = _split_key(model_key)
    label = artifact.label
    trained_at = datetime.now(timezone.utc)
    next_due = (trained_at.date() + timedelta(days=_QUARTER_DAYS))
    clean_metrics = {k: _fin(v) for k, v in (artifact.metrics or {}).items()}
    rank_ic = _fin(clean_metrics.get("rank_ic_mean"))
    ts, te = (artifact.train_range or (None, None))
    rows = []
    for ticker, er in cross.items():
        rows.append((jurisdiction, str(ticker), model_key, label, country, None, float(er),
                     True, trained_at, next_due))

    with connect() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute(
            """
            INSERT INTO quant_alpha_model (model_key, jurisdiction, country_code, label, version,
                trained_at, train_start, train_end, rank_ic, n_train_names, coverage_count,
                status, next_due, updated_at, diagnostics_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'trained',%s, now(), %s)
            ON CONFLICT (model_key, label) DO UPDATE SET
                jurisdiction=EXCLUDED.jurisdiction, country_code=EXCLUDED.country_code,
                label=EXCLUDED.label, version=EXCLUDED.version, trained_at=EXCLUDED.trained_at,
                train_start=EXCLUDED.train_start, train_end=EXCLUDED.train_end, rank_ic=EXCLUDED.rank_ic,
                n_train_names=EXCLUDED.n_train_names, coverage_count=EXCLUDED.coverage_count,
                status='trained', next_due=EXCLUDED.next_due, updated_at=now(),
                diagnostics_json=EXCLUDED.diagnostics_json
            """,
            (model_key, jurisdiction, country, artifact.label, artifact.trained_at, trained_at,
             ts, te, rank_ic, len(cross), len(cross), next_due, json.dumps(clean_metrics)),
        )
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO quant_alpha_coverage (jurisdiction, ticker, model_key, label, country_code,
                    last_as_of, expected_return, covered, last_trained_at, next_due)
                VALUES %s
                ON CONFLICT (jurisdiction, ticker, label) DO UPDATE SET
                    model_key=EXCLUDED.model_key, country_code=EXCLUDED.country_code,
                    expected_return=EXCLUDED.expected_return, covered=EXCLUDED.covered,
                    last_trained_at=EXCLUDED.last_trained_at, next_due=EXCLUDED.next_due, updated_at=now()
                """,
                rows,
            )
        conn.commit()
    return {"model_key": model_key, "label": label, "rank_ic": rank_ic, "coverage": len(cross)}


# --------------------------------------------------------------------------- worker
def _train_one(model_key: str, label: str, start: str | None, threads: int) -> dict[str, Any]:
    """Train + persist one model and upsert its ledger rows (runs in its own process)."""
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    from api.quant import qlib_alpha
    min_names = _INTL_MIN_NAMES if model_key.upper().startswith("INTL:") else 30
    try:
        artifact = qlib_alpha.train_and_save(model_key, start=start, label=label,
                                             min_names_per_date=min_names, params={"num_threads": threads})
        cross = qlib_alpha.predict_cross_section(artifact)
        rec = _record(model_key, artifact, cross)
        rec["ok"] = True
        return rec
    except Exception as exc:  # noqa: BLE001 - one model failing must not sink the batch
        logger.exception("train failed for %s", model_key)
        return {"model_key": model_key, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from api.quant.qlib_data import LABEL_HORIZONS

    p = argparse.ArgumentParser(description="train all alpha models (markets x horizons) in parallel + update the ledger")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="US + JP + every INTL country >= --min-firms (default)")
    g.add_argument("--due", action="store_true", help="only models past their quarterly next_due")
    g.add_argument("--models", type=str, default=None, help="explicit comma-separated keys, e.g. US,INTL:DE")
    p.add_argument("--min-firms", type=int, default=_DEFAULT_MIN_FIRMS)
    p.add_argument("--horizons", default=",".join(LABEL_HORIZONS),
                   help=f"comma-separated forward-return horizons; choose from {','.join(LABEL_HORIZONS)} (default: all)")
    p.add_argument("--start", default=None, help="panel start (default: ~8y back)")
    p.add_argument("--jobs", type=int, default=0, help="parallel processes (0 = min(#jobs, cpu))")
    args = p.parse_args(argv)

    if args.models:
        models = [m.strip().upper() for m in args.models.split(",") if m.strip()]
    else:
        models = all_model_keys(args.min_firms)
    horizons = [h.strip() for h in args.horizons.split(",") if h.strip() in LABEL_HORIZONS]
    if not horizons:
        print(f"no valid horizons in {args.horizons!r}; choose from {LABEL_HORIZONS}")
        return 2
    jobs = [(mk, lbl) for mk in models for lbl in horizons]
    if args.due:
        jobs = _due_jobs(jobs)
    if not jobs:
        print("nothing to train (all up to date).")
        return 0

    start = args.start or (date.today() - timedelta(days=365 * 8)).isoformat()
    cpu = os.cpu_count() or 4
    n_workers = args.jobs or min(len(jobs), cpu)
    threads = max(1, cpu // max(1, n_workers))
    print(f"training {len(jobs)} model(s) [{len(models)} markets x {len(horizons)} horizons] "
          f"on {n_workers} workers x {threads} threads (cpu={cpu})")

    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = {ex.submit(_train_one, mk, lbl, start, threads): (mk, lbl) for (mk, lbl) in jobs}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            tag = "ok" if r.get("ok") else "FAIL"
            extra = (f"rank_ic={r.get('rank_ic'):.3f} coverage={r.get('coverage')}"
                     if r.get("ok") and r.get("rank_ic") is not None else r.get("error", ""))
            print(f"  [{tag}] {r['model_key']} {r.get('label', '')}: {extra}")

    ok = sum(1 for r in results if r.get("ok"))
    print(f"done: {ok}/{len(results)} trained; ledger updated (quant_alpha_model / quant_alpha_coverage).")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
