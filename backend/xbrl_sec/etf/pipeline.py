"""ETF pipeline orchestrator (WA0006 §4.1). Stages: FIRDS -> Xetra -> prices."""
from __future__ import annotations

from datetime import date, datetime, timezone

from . import firds
from .prices import fetch_etf_prices
from .writers import active_etfs, recompute_primary_listings, record_firds_run, upsert_etfs, upsert_listings
from .xetra import enrich_from_xetra


def _today() -> datetime:
    return datetime.now(timezone.utc)


def run_firds(
    file_type: str = "FULINS",
    instrument_letter: str | None = firds.ETF_INSTRUMENT_LETTER,
    limit: int | None = None,
    max_scan: int | None = None,
) -> dict:
    """Stage 1: discover the latest FIRDS file, parse it, upsert DE/AT ETFs."""
    started = _today()
    files = firds.discover_firds_files(file_type=file_type, instrument_letter=instrument_letter)
    if not files:
        raise RuntimeError(f"No {file_type} files found in ESMA FIRDS index")
    latest = files[0]
    url = latest.get("download_link") or latest.get("download_url")
    fname = latest.get("file_name")
    if not url:
        raise RuntimeError(f"FIRDS doc has no download link: {latest!r}")

    file_date = date.today()
    pub = (latest.get("publication_date") or "")[:10]
    try:
        file_date = date.fromisoformat(pub)
    except ValueError:
        pass

    try:
        path = firds.download_file(url, fname)
        etfs, listings, scanned = firds.parse_firds_zip(path, limit=limit, max_scan=max_scan)
        n_etf = upsert_etfs(etfs)
        n_list = upsert_listings(listings)
        recompute_primary_listings()
        record_firds_run(
            file_type, file_date, url, None, "complete", scanned, n_etf, started, _today(),
        )
        return {
            "file": fname, "instruments_scanned": scanned,
            "etfs_upserted": n_etf, "listings_upserted": n_list,
        }
    except Exception as exc:  # noqa: BLE001
        record_firds_run(file_type, file_date, url, None, "failed", None, None, started, _today(), str(exc)[:300])
        raise


def run_xetra() -> dict:
    """Stage 2: enrich dim_etf from the Xetra ETF CSV (best-effort)."""
    return enrich_from_xetra()


def run_prices(
    limit: int | None = None,
    period: str = "max",
    *,
    only_missing: bool = False,
    isin: str | None = None,
    use_fallbacks: bool = True,
    allow_licensed_justetf: bool = False,
    licensed_justetf_dir: str | None = None,
) -> dict:
    """Stage 3: fetch daily prices for active ETFs from yfinance."""
    pairs = active_etfs(
        limit=limit,
        only_missing_prices=only_missing,
        isins=[isin] if isin else None,
    )
    if not pairs:
        return {"requested": 0, "ok": 0, "empty": 0, "failed": 0, "rows": 0}
    return fetch_etf_prices(
        pairs,
        period=period,
        use_fallbacks=use_fallbacks,
        allow_licensed_justetf=allow_licensed_justetf,
        licensed_justetf_dir=licensed_justetf_dir,
    )


def run_all(limit: int | None = None, period: str = "max", max_scan: int | None = None) -> dict:
    out = {}
    out["firds"] = run_firds(limit=limit, max_scan=max_scan)
    out["xetra"] = run_xetra()
    out["prices"] = run_prices(limit=limit, period=period)
    return out


def status() -> dict:
    from xbrl_sec.sec.db.connection import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sec.dim_etf")
        etfs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM sec.dim_etf_listing")
        listings = cur.fetchone()[0]
        cur.execute("SELECT count(*), count(DISTINCT isin) FROM sec.fact_prices_etf")
        prices, priced_isins = cur.fetchone()
        cur.execute("SELECT count(*) FROM sec.pipeline_firds_run")
        runs = cur.fetchone()[0]
    return {
        "dim_etf": etfs, "dim_etf_listing": listings,
        "fact_prices_etf": prices, "priced_isins": priced_isins, "firds_runs": runs,
    }
