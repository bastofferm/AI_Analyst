"""Extract stock-split events from SEC Form 8-K text.

Pre-filters to 8-K filings whose `items[]` array overlaps {'5.03','8.01'},
which captures essentially all formally-announced US stock splits. The
body HTML must already be downloaded (eightk_ingest --download).

Pattern matching looks for split-announcement prose in three forms:

  * "N-for-M [forward/reverse] stock split"     → ratio = N / M
  * "split into N shares of common stock"       → ratio = N (assumed N-for-1)
  * "reverse stock split at a ratio of 1-for-K" → ratio = 1/K

Splits write into `sec.fact_stock_split_event` with `source_type='SEC_8K'`.
A `confidence` score reflects how cleanly the ratio + effective date were
extracted; high-confidence rows can dominate over the existing yfinance
rows (`confidence=0.95`) at downstream join time.

CLI:
    python -m xbrl_sec.sec.sources.eightk_splits_ingest --full
    python -m xbrl_sec.sec.sources.eightk_splits_ingest --incremental
    python -m xbrl_sec.sec.sources.eightk_splits_ingest --accession 0001193125-20-213158
"""
from __future__ import annotations

import argparse
import html as _html
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterator

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_WORD_TO_INT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50,
    "one hundred": 100, "hundred": 100,
}


def _word_or_int(token: str) -> int | None:
    token = token.strip().lower().replace(" ", " ").replace("-", "-")
    if token.isdigit():
        return int(token)
    # Allow hyphenated/spaced two-word numbers e.g. "twenty-five"
    return _WORD_TO_INT.get(token) or _WORD_TO_INT.get(token.replace("-", " "))


def _strip_html(html_text: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html_text)
    s = _html.unescape(s)
    s = s.replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_NUM_TOKEN = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|twenty-five|thirty|forty|fifty|hundred|one hundred)"

# "N-for-M stock split" / "N for M split" / "N-to-M split"
_RATIO_FOR_RE = re.compile(
    rf"({_NUM_TOKEN})[\s -]+(?:for|to)[\s -]+({_NUM_TOKEN})\b"
    rf"(?:[^.]{{0,80}}?\b(?:stock|share|forward|reverse|stock\s+split|split)\b)",
    re.IGNORECASE,
)

# "split into N shares" — implies N-for-1
_RATIO_INTO_RE = re.compile(
    rf"\bsplit\s+into\s+({_NUM_TOKEN})\s+shares?\b",
    re.IGNORECASE,
)

# Reverse-split-specific
_REVERSE_HINT_RE = re.compile(r"\breverse\s+(?:stock\s+)?split\b", re.IGNORECASE)

# "effective ... [Month Day, Year]" / "on Month Day, Year, ..."
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)

_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date_near(text: str, anchor_pos: int, window: int = 800) -> date | None:
    """Return the month-day-year date CLOSEST to `anchor_pos` (within ±`window`).

    8-K split filings typically mention several dates (filing date, board-approval
    date, effective date). The effective date is the one in proximity to the
    "split" keyword. We pick the closest match to the anchor.
    """
    s = max(0, anchor_pos - window)
    e = min(len(text), anchor_pos + window)
    best: tuple[int, date] | None = None  # (distance, date)
    for m in _DATE_RE.finditer(text, s, e):
        try:
            mon = _MONTH_NUM[m.group(1).lower()]
            day = int(m.group(2))
            year = int(m.group(3))
            d = date(year, mon, day)
        except (KeyError, ValueError):
            continue
        dist = min(abs(m.start() - anchor_pos), abs(m.end() - anchor_pos))
        if best is None or dist < best[0]:
            best = (dist, d)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Per-file extraction
# ---------------------------------------------------------------------------

def _extract_splits(text: str) -> list[dict]:
    """Return zero or more split records: {ratio, is_reverse, effective_date, confidence, snippet}."""
    out: list[dict] = []

    def _add(ratio: float, eff: date | None, snippet: str, confidence: float, is_reverse: bool):
        if ratio <= 0 or ratio == 1.0:
            return
        # Sanity: real splits are between ~0.05 (1-for-20 reverse) and 100 (100-for-1 fwd)
        if ratio > 200 or ratio < 0.01:
            return
        out.append({
            "split_ratio":    ratio,
            "is_reverse":     is_reverse,
            "effective_date": eff,
            "snippet":        snippet[:300],
            "confidence":     confidence,
        })

    # Pattern 1: "N-for-M split"
    for m in _RATIO_FOR_RE.finditer(text):
        n = _word_or_int(m.group(1))
        d = _word_or_int(m.group(2))
        if n is None or d is None or d == 0:
            continue
        # Determine forward vs reverse from surrounding text
        ctx = text[max(0, m.start()-120): m.end()+120]
        is_reverse = bool(_REVERSE_HINT_RE.search(ctx)) or (n == 1 and d > 1)
        ratio = (n / d) if is_reverse else (n / d)
        # n/d works for both: 4-for-1 → 4.0 fwd; 1-for-10 → 0.1 reverse
        eff = _parse_date_near(text, m.start())
        conf = 0.99 if eff is not None else 0.85
        _add(ratio, eff, ctx, conf, is_reverse)

    # Pattern 2: "split into N shares"
    for m in _RATIO_INTO_RE.finditer(text):
        n = _word_or_int(m.group(1))
        if n is None or n <= 1:
            continue
        ctx = text[max(0, m.start()-120): m.end()+120]
        eff = _parse_date_near(text, m.start())
        conf = 0.95 if eff is not None else 0.80
        _add(float(n), eff, ctx, conf, is_reverse=False)

    # A single 8-K filing announces at most one split. If multiple rows survive,
    # keep the one with the highest confidence; tie-break by ratio (prefer real
    # split ratios — i.e. between 2 and 100). This suppresses spurious matches
    # like "shareholders of record on <unrelated date>".
    if not out:
        return []
    out.sort(key=lambda r: (r["confidence"], 1.0 / (1.0 + abs(r["split_ratio"] - 1))),
             reverse=True)
    return [out[0]]


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO sec.fact_stock_split_event
    (jurisdiction, entity_id, ticker, event_date, effective_date,
     split_ratio, source_type, source_filing_id, confidence, notes)
VALUES %s
ON CONFLICT (jurisdiction, ticker, effective_date, source_type)
DO UPDATE SET
    entity_id        = EXCLUDED.entity_id,
    event_date       = EXCLUDED.event_date,
    split_ratio      = EXCLUDED.split_ratio,
    source_filing_id = EXCLUDED.source_filing_id,
    confidence       = EXCLUDED.confidence,
    notes            = EXCLUDED.notes,
    updated_at       = now()
"""


def _ticker_for_cik(cik: str) -> str | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT primary_ticker FROM sec.dim_company_us WHERE cik = %s",
                    (cik,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------------------------

_FETCH_PENDING_SQL = """
SELECT cik, accession, filed_date, local_path
FROM   sec.dim_eightk_filing
WHERE  downloaded_at IS NOT NULL
  AND  local_path IS NOT NULL
  AND  items && ARRAY['5.03','8.01']::text[]
  {already_filter}
ORDER  BY filed_date DESC
"""


def _read_local(local_path_str: str) -> str | None:
    """Read a downloaded 8-K body.

    `local_path_str` is normally an absolute path (post-G2). Legacy rows that
    still carry a relative path (`us_sec/eightk/...`) are resolved against
    `market_data_root` for backwards compatibility.
    """
    p = Path(local_path_str)
    if not p.is_absolute():
        p = load_settings().market_data_root / p
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def ingest(incremental: bool = False, accession: str | None = None) -> dict:
    """Walk downloaded 8-K bodies, extract splits, write to fact_stock_split_event."""
    if accession:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT cik, accession, filed_date, local_path
                FROM   sec.dim_eightk_filing
                WHERE  accession = %s AND downloaded_at IS NOT NULL
            """, (accession,))
            rows = cur.fetchall()
    else:
        already_filter = ""
        if incremental:
            already_filter = "AND parsed_at IS NULL"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(_FETCH_PENDING_SQL.format(already_filter=already_filter))
            rows = cur.fetchall()

    logger.info("processing %d 8-K filings", len(rows))
    if not rows:
        return {"filings_seen": 0, "filings_with_split": 0, "rows_written": 0}

    db_rows: list[tuple] = []
    parsed_acc: list[str] = []
    filings_with_split = 0

    ticker_cache: dict[str, str | None] = {}

    for cik, accession_id, filed_date, local_path in rows:
        text_html = _read_local(local_path)
        if text_html is None:
            continue
        text = _strip_html(text_html)
        splits = _extract_splits(text)
        parsed_acc.append(accession_id)
        if not splits:
            continue

        ticker = ticker_cache.get(cik)
        if ticker is None and cik not in ticker_cache:
            ticker = _ticker_for_cik(cik)
            ticker_cache[cik] = ticker
        if not ticker:
            continue

        filings_with_split += 1
        for s in splits:
            eff = s["effective_date"] or filed_date
            db_rows.append((
                "US",
                cik,
                ticker,
                filed_date,
                eff,
                s["split_ratio"],
                "SEC_8K",
                accession_id,
                s["confidence"],
                s["snippet"],
            ))

    if db_rows:
        # Dedupe within batch by (ticker, effective_date) — keep highest confidence
        by_key: dict[tuple, tuple] = {}
        for r in db_rows:
            key = (r[2], r[4])  # ticker, effective_date
            prev = by_key.get(key)
            if prev is None or (r[8] or 0) > (prev[8] or 0):
                by_key[key] = r
        payload = list(by_key.values())
        with connect() as conn, conn.cursor() as cur:
            execute_values(cur, _UPSERT_SQL, payload, page_size=500)

    if parsed_acc:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE sec.dim_eightk_filing
                SET    parsed_at = now()
                WHERE  accession = ANY(%s)
            """, (parsed_acc,))

    return {
        "filings_seen":       len(rows),
        "filings_with_split": filings_with_split,
        "rows_written":       len(db_rows),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse SEC Form 8-K text for stock-split events.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full",        action="store_true")
    mode.add_argument("--incremental", action="store_true")
    parser.add_argument("--accession", type=str, default=None,
                        help="Restrict to a single accession number")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stats = ingest(incremental=args.incremental, accession=args.accession)
    logger.info("done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
