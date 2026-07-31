"""Backfill sec.ref_xbrl_relationship_edge from already-extracted linkbase XML.

Walks the on-disk linkbase archives for US and/or JP filings and populates
the normalized relationship edge table. Idempotent — re-running for the
same filing updates rows in place via the natural-key unique index.

Usage::

    python -m xbrl_sec.sec.scripts.backfill_relationship_edges --jurisdiction US
    python -m xbrl_sec.sec.scripts.backfill_relationship_edges --jurisdiction JP
    python -m xbrl_sec.sec.scripts.backfill_relationship_edges --jurisdiction BOTH

For US, ``--entity-ids 0000320193,0000789019`` scopes the run to specific
CIKs. For JP, ``--entity-ids E12345,E67890`` scopes the run to EDINET codes.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.writers.relationship_edges import write_filing_edges


def _us_linkbase_paths(cik: str, accession: str) -> tuple[Path | None, Path | None, Path | None]:
    """Return (cal_path, pre_path, def_path) for a US filing on disk."""
    root = load_settings().market_data_root / "us_sec"
    stem = f"CIK{str(cik).zfill(10)}_{accession}"
    cal = root / "xbrl_cal" / f"{stem}_cal.xml"
    pre = root / "xbrl_pre" / f"{stem}_pre.xml"
    df = root / "xbrl_def" / f"{stem}_def.xml"
    return (
        cal if cal.exists() else None,
        pre if pre.exists() else None,
        df if df.exists() else None,
    )


def _iter_us_filings(entity_ids: list[str] | None) -> Iterable[tuple[str, str]]:
    """Yield (cik, filing_id) tuples for US filings recorded as extracted."""
    params: list = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND entity_id = ANY(%s)"
        params.append(entity_ids)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT entity_id, filing_id
            FROM source_filing_state
            WHERE jurisdiction = 'US'
              AND COALESCE(extracted, FALSE) = TRUE
              {entity_filter}
            ORDER BY entity_id, filing_id
            """,
            params,
        )
        for entity_id, filing_id in cur.fetchall():
            yield str(entity_id), str(filing_id)


def backfill_us(entity_ids: list[str] | None = None, progress_every: int = 100) -> dict[str, int]:
    """Backfill US relationship edges for already-extracted filings."""
    stats = {"filings_seen": 0, "filings_with_edges": 0, "edges_written": 0, "filings_missing_files": 0}
    for cik, accession in _iter_us_filings(entity_ids):
        stats["filings_seen"] += 1
        cal_path, pre_path, def_path = _us_linkbase_paths(cik, accession)
        if not (cal_path or pre_path or def_path):
            stats["filings_missing_files"] += 1
            continue
        written = write_filing_edges(
            jurisdiction="US",
            entity_id=cik,
            filing_id=accession,
            taxonomy=None,
            cal_path=cal_path,
            pre_path=pre_path,
            def_path=def_path,
        )
        if written:
            stats["filings_with_edges"] += 1
            stats["edges_written"] += written
        if progress_every and stats["filings_seen"] % progress_every == 0:
            print(
                f"  US progress: {stats['filings_seen']} filings seen, "
                f"{stats['edges_written']} edges written"
            )
    return stats


def _iter_jp_filings(entity_ids: list[str] | None):
    """Yield EdinetXbrlFile objects for parsed JP filings.

    Importing locally to avoid pulling EDINET-only code paths during US-only runs.
    ``hash_files=False`` because we only care about linkbase companion paths;
    skipping the full SHA per file shaves a lot of I/O off the discover step.
    """
    from xbrl_sec.sec.sources.edinet_filings import discover_xbrl_files

    items = discover_xbrl_files(entity_ids=entity_ids, hash_files=False)
    for item in items:
        yield item


def backfill_jp(entity_ids: list[str] | None = None, progress_every: int = 100) -> dict[str, int]:
    """Backfill JP relationship edges from EDINET filing companions."""
    stats = {"filings_seen": 0, "filings_with_edges": 0, "edges_written": 0, "filings_missing_files": 0}
    try:
        iterator = list(_iter_jp_filings(entity_ids))
    except Exception as exc:  # pragma: no cover - depends on EDINET archive presence
        print(f"  JP backfill skipped: {exc}")
        return stats
    for item in iterator:
        stats["filings_seen"] += 1
        cal_path = getattr(item, "cal_path", None)
        pre_path = getattr(item, "pre_path", None)
        def_path = getattr(item, "def_path", None)
        if not (cal_path or pre_path or def_path):
            stats["filings_missing_files"] += 1
            continue
        written = write_filing_edges(
            jurisdiction="JP",
            entity_id=getattr(item, "edinet_code", None),
            filing_id=getattr(item, "filing_id", None) or getattr(item, "doc_id", None),
            taxonomy=getattr(item, "taxonomy", None),
            cal_path=cal_path,
            pre_path=pre_path,
            def_path=def_path,
        )
        if written:
            stats["filings_with_edges"] += 1
            stats["edges_written"] += written
        if progress_every and stats["filings_seen"] % progress_every == 0:
            print(
                f"  JP progress: {stats['filings_seen']} filings seen, "
                f"{stats['edges_written']} edges written"
            )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jurisdiction", choices=("US", "JP", "BOTH"), default="BOTH")
    parser.add_argument("--entity-ids", default="", help="Comma-separated CIKs or EDINET codes")
    parser.add_argument("--progress-every", type=int, default=100)
    args = parser.parse_args()
    entity_ids = [x.strip() for x in args.entity_ids.split(",") if x.strip()] or None

    if args.jurisdiction in ("US", "BOTH"):
        print("Backfilling US relationship edges...")
        stats = backfill_us(entity_ids=entity_ids, progress_every=args.progress_every)
        print(f"  US done: {stats}")
    if args.jurisdiction in ("JP", "BOTH"):
        print("Backfilling JP relationship edges...")
        stats = backfill_jp(entity_ids=entity_ids, progress_every=args.progress_every)
        print(f"  JP done: {stats}")


if __name__ == "__main__":
    main()
