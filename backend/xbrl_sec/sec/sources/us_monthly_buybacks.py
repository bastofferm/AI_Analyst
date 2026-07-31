"""Parse 'Issuer Purchases of Equity Securities' tables from US 10-K / 10-Q.

Each filing reports buybacks broken down by month (or fiscal "period") within
the quarter. The data lives in an HTML table in Item 5 (10-K) or Item 2(c)
(10-Q), embedded in the iXBRL HTML body but NOT individually XBRL-tagged.

Layout typically looks like:

    | Period                           | Total Shares | Avg Price | Under Plan | Max Remaining |
    | June 30 - August 3, 2019         | 30,746       | $204.85   | 30,746     | $80B          |
    | August 4 - August 31, 2019       | 41,591       | $200.52   | 41,591     | $76B          |
    | September 1 - September 28, 2019 | 24,887       | $215.10   | 24,887     | $71B          |
    | Total                            | 97,224       | $206.49   | 97,224     |               |

Apple-style filings nest multiple rows per period (ASR, open-market) — we
sum across all rows between two period labels.

UPSERTs into sec.fact_us_monthly_buybacks. CLI:
    python -m xbrl_sec.sec.sources.us_monthly_buybacks --full
    python -m xbrl_sec.sec.sources.us_monthly_buybacks --incremental
    python -m xbrl_sec.sec.sources.us_monthly_buybacks --cik 0000320193
"""
from __future__ import annotations

import argparse
import calendar
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Iterator

from lxml import html as lxml_html

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

def _xbrl_html_root() -> Path:
    return load_settings().project_root / "market_data" / "us_sec" / "xbrl_html"


# Filename pattern: CIK0000320193_0000320193-19-000119.htm
_FILENAME_RE = re.compile(
    r"^CIK(?P<cik>\d{10})_(?P<accession>\d{10}-\d{2}-\d{6})\.htm$",
    re.IGNORECASE,
)


def _iter_files(cik_filter: str | None = None) -> Iterator[Path]:
    root = _xbrl_html_root()
    if not root.exists():
        return
    for p in root.iterdir():
        if not p.is_file() or p.suffix.lower() != ".htm":
            continue
        if cik_filter:
            m = _FILENAME_RE.match(p.name)
            if not m or m.group("cik").lstrip("0") != cik_filter.lstrip("0"):
                continue
        yield p


# ---------------------------------------------------------------------------
# Filing form lookup
# ---------------------------------------------------------------------------

def _filing_forms_for_files(paths: list[Path]) -> dict[str, dict]:
    """Bulk-lookup filing metadata from source_filing_state for a batch.

    source_filing_state schema: jurisdiction, filing_id, entity_id (=cik for US),
    filing_type (=form), filed_date, period_end.
    """
    if not paths:
        return {}
    accessions = []
    for p in paths:
        m = _FILENAME_RE.match(p.name)
        if m:
            accessions.append(m.group("accession"))
    if not accessions:
        return {}
    out: dict[str, dict] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT filing_id, entity_id, filing_type, filed_date, period_end
            FROM   sec.source_filing_state
            WHERE  jurisdiction = 'US' AND filing_id = ANY(%s)
        """, (accessions,))
        for filing_id, entity_id, filing_type, filed_date, period_end in cur.fetchall():
            out[filing_id] = {
                "cik":               str(entity_id).zfill(10),
                "form":              filing_type,
                "filed_date":        filed_date,
                "period_of_report":  period_end,
            }
    return out


# ---------------------------------------------------------------------------
# Locate the "Issuer Purchases of Equity Securities" table
# ---------------------------------------------------------------------------

_HEADER_PATTERNS = (
    re.compile(r"Issuer\s+Purchases\s+of\s+Equity\s+Securit", re.IGNORECASE),
    re.compile(r"Purchases\s+of\s+Equity\s+Securities\s+by\s+the\s+Issuer", re.IGNORECASE),
)

# Anchors that indicate the table itself (vs a TOC line that mentions Item 5).
_TABLE_HEADER_HINTS = (
    "Total Number of Shares",
    "Total Number of Shares Purchased",
    "Average Price",
    "Approximate Dollar Value",
)

# Unit-qualifier phrases that determine the multiplier for share counts.
# Matched in the text BEFORE the table (the preamble).
_SHARES_IN_THOUSANDS_RE = re.compile(
    r"shares?[^.]{0,80}?(?:are\s+)?(?:reflected|presented|stated|expressed)?\s*"
    r"in\s+thousands",
    re.IGNORECASE,
)
_SHARES_IN_MILLIONS_RE = re.compile(
    r"shares?[^.]{0,80}?(?:are\s+)?(?:reflected|presented|stated|expressed)?\s*"
    r"in\s+millions",
    re.IGNORECASE,
)
# Generic "(in thousands)" near the table — applies to share counts unless an
# "except shares" clause overrides it.
_GENERIC_THOUSANDS_RE = re.compile(r"\(\s*in\s+thousands", re.IGNORECASE)
_GENERIC_MILLIONS_RE  = re.compile(r"\(\s*in\s+millions",  re.IGNORECASE)
_EXCEPT_SHARES_RE     = re.compile(r"except\s+(?:number\s+of\s+)?shares?", re.IGNORECASE)


def _detect_share_scale(preamble_html: str) -> int:
    """Return the multiplier (1, 1000, or 1_000_000) for share-count cells
    based on phrases like '(in thousands)' or 'shares in thousands'.

    AAPL's typical preamble: "in millions, except number of shares, which are
    reflected in thousands, and per share amounts" → returns 1000.
    """
    txt = re.sub(r"<[^>]+>", " ", preamble_html)
    txt = re.sub(r"&#?\w+;", " ", txt)
    txt = re.sub(r"\s+", " ", txt)

    # Explicit "shares ... in thousands/millions" wins.
    if _SHARES_IN_THOUSANDS_RE.search(txt):
        return 1000
    if _SHARES_IN_MILLIONS_RE.search(txt):
        return 1_000_000

    # Generic "(in thousands)" — apply to shares unless explicitly excepted.
    if _GENERIC_THOUSANDS_RE.search(txt):
        if _EXCEPT_SHARES_RE.search(txt):
            return 1
        return 1000
    if _GENERIC_MILLIONS_RE.search(txt):
        if _EXCEPT_SHARES_RE.search(txt):
            return 1
        return 1_000_000

    return 1


def _find_table_html(html_text: str) -> tuple[str, str] | None:
    """Return (preamble, table_html) for the Item 5 / Item 2(c) buyback table.

    Preamble is the ~2000 chars between the section header and the table —
    used to detect "(in thousands)" qualifiers that scale share counts.
    """
    matches: list[tuple[int, int]] = []
    for pat in _HEADER_PATTERNS:
        matches.extend((m.start(), m.end()) for m in pat.finditer(html_text))
    if not matches:
        return None

    for hdr_start, hdr_end in sorted(matches):
        sub = html_text[hdr_end : hdr_end + 80_000]
        tab = re.search(r"<table\b[^>]*>(.*?)</table>", sub, re.DOTALL | re.IGNORECASE)
        if not tab:
            continue
        table_html = tab.group(0)
        hits = sum(1 for hint in _TABLE_HEADER_HINTS if hint.lower() in table_html.lower())
        if hits >= 2:
            preamble = html_text[hdr_end : hdr_end + tab.start()]
            return preamble, table_html

    return None


# ---------------------------------------------------------------------------
# Period-label parsing
# ---------------------------------------------------------------------------

_MONTH = (
    r"(?P<%s_mon>January|February|March|April|May|June|July|August|September|October|November|December)"
)
_DAY = r"(?P<%s_day>\d{1,2})"
_YEAR = r"(?P<%s_year>\d{4})"

# "June 30, 2019 to August 3, 2019" / "June 30 to August 3, 2019" / "June 30 – August 3, 2019"
_RANGE_RE = re.compile(
    rf"{_MONTH % 'a'}\s+{_DAY % 'a'}(?:,\s*{_YEAR % 'a'})?\s*"
    rf"(?:to|[-‐-―−])\s*"
    rf"{_MONTH % 'b'}\s+{_DAY % 'b'},?\s*{_YEAR % 'b'}",
    re.IGNORECASE,
)

# "July 2024" (single-month label)
_SINGLE_MONTH_RE = re.compile(
    rf"{_MONTH % 's'}\s+{_YEAR % 's'}",
    re.IGNORECASE,
)


_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_period_label(text: str) -> tuple[date, date] | None:
    """Return (period_start, period_end) parsed from a free-form label, or None."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    # Try range pattern first
    m = _RANGE_RE.search(text)
    if m:
        try:
            b_year = int(m.group("b_year"))
            a_year = int(m.group("a_year") or b_year)
            a_mon = _MONTH_NUM[m.group("a_mon").lower()]
            b_mon = _MONTH_NUM[m.group("b_mon").lower()]
            a_day = int(m.group("a_day"))
            b_day = int(m.group("b_day"))
            return date(a_year, a_mon, a_day), date(b_year, b_mon, b_day)
        except (ValueError, KeyError):
            pass
    # Single month fallback
    m = _SINGLE_MONTH_RE.search(text)
    if m:
        try:
            year = int(m.group("s_year"))
            mon = _MONTH_NUM[m.group("s_mon").lower()]
            last = calendar.monthrange(year, mon)[1]
            return date(year, mon, 1), date(year, mon, last)
        except (ValueError, KeyError):
            pass
    return None


# ---------------------------------------------------------------------------
# Numeric parsing
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _cell_text(td) -> str:
    if td is None:
        return ""
    return " ".join(td.text_content().split())


def _cell_int(td) -> int | None:
    txt = _cell_text(td)
    if not txt:
        return None
    # Drop $ signs, footnote marks, etc. Keep digit groups + commas.
    m = _NUMBER_RE.search(txt.replace("$", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _cell_num(td) -> float | None:
    txt = _cell_text(td)
    if not txt:
        return None
    m = _NUMBER_RE.search(txt.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Row classification + extraction
# ---------------------------------------------------------------------------

def _is_total_row(label: str) -> bool:
    norm = label.lower().strip()
    return norm.startswith("total") or norm == "計"


def _row_has_period_label(label: str) -> tuple[date, date] | None:
    """If label encodes a date range, return it; otherwise None."""
    return _parse_period_label(label)


def _compact_cells(tr) -> list[str]:
    """Return non-empty cell texts from a <tr>, dropping pure currency symbols
    and footnote markers like '(1)'. Many iXBRL tables stuff every numeric
    cell with width:1% spacer `<td>` columns; compacting collapses those away.
    """
    cells = tr.findall("./td") or tr.findall("./th")
    out: list[str] = []
    for td in cells:
        txt = _cell_text(td)
        if not txt:
            continue
        # Drop currency symbol cells
        if txt in {"$", "USD", "%"}:
            continue
        # Drop pure footnote markers like (1), (2)
        if re.fullmatch(r"\(\d+\)", txt):
            continue
        out.append(txt)
    return out


def _values_from_compact(compact: list[str]) -> tuple[int | None, float | None, int | None, float | None]:
    """Given the non-empty cell texts of a data row, extract:
    (shares_purchased, avg_price_paid, shares_under_plan, max_remaining_amount).

    The "Issuer Purchases" table has 4 numeric columns:
        [Total Shares, Avg Price/Share, Under Plan, Max Remaining]

    But some rows (e.g. AAPL's ASR forward-contract rows) only fill columns 1+3
    (shares + under_plan) because the ASR price is settlement-based and reported
    in a footnote, not inline. We use TWO signals to disambiguate:

      1. Decimals — price has decimals (e.g. 205.36), shares/under_plan are
         integers.
      2. Magnitude — buyback avg prices are typically $1-$5000; values > 50k
         are almost certainly share counts, not prices.
    """
    nums: list[tuple[float, bool]] = []  # (value, has_decimal)
    for txt in compact[1:]:
        m = _NUMBER_RE.search(txt.replace("$", ""))
        if not m:
            continue
        token = m.group(0)
        try:
            v = float(token.replace(",", ""))
        except ValueError:
            continue
        nums.append((v, "." in token))

    if not nums:
        return None, None, None, None

    # Column 1 (shares) is always the first numeric.
    shares = int(nums[0][0]) if nums[0][0] >= 0 else None

    # Try to locate the price as the first remaining numeric with decimals AND a
    # sensible magnitude (< 50000). If none qualifies, this row has no inline
    # price (e.g. ASR sub-row).
    price: float | None = None
    price_idx: int | None = None
    for i in range(1, len(nums)):
        v, has_dec = nums[i]
        if has_dec and 0 < v < 50_000:
            price = v
            price_idx = i
            break

    # Remaining integer columns map to under_plan and max_remaining.
    leftover = [nums[i] for i in range(1, len(nums)) if i != price_idx]
    under: int | None = None
    max_rem: float | None = None
    if leftover:
        under = int(leftover[0][0])
    if len(leftover) >= 2:
        # max_rem is typically the dollar amount of remaining authorization
        max_rem = float(leftover[1][0])

    return shares, price, under, max_rem


def _extract_buyback_rows(table_html: str, share_scale: int = 1) -> list[dict]:
    """Walk table rows, accumulate one record per period (summing sub-rows).

    `share_scale` is the multiplier (1, 1000, 1_000_000) applied to share-count
    columns. Detected from the table preamble via `_detect_share_scale`.
    """
    try:
        root = lxml_html.fromstring(table_html)
    except Exception as exc:
        logger.debug("table parse error: %s", exc)
        return []

    rows = root.findall(".//tr")
    out: list[dict] = []
    current: dict | None = None
    total_shares_weight = 0.0

    def _flush():
        nonlocal current, total_shares_weight
        if current is None:
            return
        if total_shares_weight > 0:
            current["avg_price_paid_per_share"] = current["_price_num"] / total_shares_weight
        else:
            current["avg_price_paid_per_share"] = current.get("_avg_price_first")
        for k in ("_price_num", "_avg_price_first"):
            current.pop(k, None)
        out.append(current)
        current = None
        total_shares_weight = 0.0

    for tr in rows:
        compact = _compact_cells(tr)
        if not compact:
            continue
        label = compact[0]
        period = _row_has_period_label(label)
        is_total = _is_total_row(label)

        if period is not None:
            _flush()
            current = {
                "period_start": period[0],
                "period_end":   period[1],
                "shares_purchased": 0,
                "shares_under_program_remaining": None,
                "program_max_remaining_amount":   None,
                "_price_num": 0.0,
                "_avg_price_first": None,
            }
            shares, price, under, max_rem = _values_from_compact(compact)
            if shares is not None and shares > 0:
                shares *= share_scale
                current["shares_purchased"] += shares
                if price is not None:
                    current["_price_num"] += shares * price
                    total_shares_weight += shares
            elif price is not None:
                current["_avg_price_first"] = price
            if under is not None:
                current["shares_under_program_remaining"] = under * share_scale
            if max_rem is not None:
                current["program_max_remaining_amount"] = max_rem
            continue

        if is_total:
            _flush()
            continue

        # Sub-row of the current period (e.g. AAPL's ASR vs Open-Market rows)
        if current is None:
            continue
        shares, price, under, max_rem = _values_from_compact(compact)
        if shares is not None and shares > 0:
            shares *= share_scale
            current["shares_purchased"] += shares
            if price is not None:
                current["_price_num"] += shares * price
                total_shares_weight += shares
        if under is not None:
            under_scaled = under * share_scale
            current["shares_under_program_remaining"] = (
                (current.get("shares_under_program_remaining") or 0) + under_scaled
            )
        if max_rem is not None and current["program_max_remaining_amount"] is None:
            current["program_max_remaining_amount"] = max_rem

    _flush()
    return [r for r in out if (r["shares_purchased"] or 0) > 0]


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO sec.fact_us_monthly_buybacks
    (cik, period_start, period_end,
     shares_purchased, avg_price_paid_per_share,
     shares_under_program_remaining, program_max_remaining_amount,
     filing_form, filing_id, filed_date)
VALUES %s
ON CONFLICT (cik, period_start, period_end, filing_id)
DO UPDATE SET
    shares_purchased               = EXCLUDED.shares_purchased,
    avg_price_paid_per_share       = EXCLUDED.avg_price_paid_per_share,
    shares_under_program_remaining = EXCLUDED.shares_under_program_remaining,
    program_max_remaining_amount   = EXCLUDED.program_max_remaining_amount,
    filing_form                    = EXCLUDED.filing_form,
    filed_date                     = EXCLUDED.filed_date,
    updated_at                     = now()
"""


def _write_rows(rows: list[tuple]) -> int:
    if not rows:
        return 0
    # Dedupe within the batch by PK (cik, period_start, period_end, filing_id).
    # A single Item 5 table can occasionally produce two rows with the same
    # period span (e.g. parser mis-aligns a sub-row label as a new period).
    # Keep the last occurrence.
    by_key: dict[tuple, tuple] = {}
    for r in rows:
        # PK columns are 0=cik, 1=period_start, 2=period_end, 8=filing_id
        by_key[(r[0], r[1], r[2], r[8])] = r
    payload = list(by_key.values())
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, _UPSERT_SQL, payload, page_size=500)
    return len(payload)


# ---------------------------------------------------------------------------
# Per-file extract
# ---------------------------------------------------------------------------

def _extract_for_file(path: Path, meta: dict) -> list[tuple]:
    """Read file, locate table, parse rows, return DB tuples."""
    try:
        html_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("read error %s: %s", path.name, exc)
        return []

    # Cheap pre-filter to skip obvious non-buyback filings
    if not any(p.search(html_text) for p in _HEADER_PATTERNS):
        return []

    found = _find_table_html(html_text)
    if found is None:
        return []
    preamble, table_html = found

    share_scale = _detect_share_scale(preamble)
    period_rows = _extract_buyback_rows(table_html, share_scale=share_scale)
    if not period_rows:
        return []

    accession = _FILENAME_RE.match(path.name).group("accession")
    return [
        (
            meta["cik"],
            r["period_start"],
            r["period_end"],
            r["shares_purchased"] or None,
            r["avg_price_paid_per_share"],
            r["shares_under_program_remaining"],
            r["program_max_remaining_amount"],
            meta["form"],
            accession,
            meta["filed_date"],
        )
        for r in period_rows
    ]


# ---------------------------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------------------------

def ingest(cik_filter: str | None = None, incremental: bool = False) -> dict:
    """Walk xbrl_html, extract buyback tables, write to fact_us_monthly_buybacks."""

    paths = list(_iter_files(cik_filter=cik_filter))
    if not paths:
        return {"files_seen": 0, "rows_written": 0, "files_with_data": 0, "files_skipped": 0}

    seen_filings: set[str] = set()
    if incremental:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT filing_id FROM sec.fact_us_monthly_buybacks")
            seen_filings = {r[0] for r in cur.fetchall()}
        logger.info("incremental mode: %d filings already in DB", len(seen_filings))

    # Bulk-lookup filing_form / filed_date for all paths up-front (avoids per-file
    # queries). One query for all accessions.
    forms_map = _filing_forms_for_files(paths)

    batch: list[tuple] = []
    files_with_data = 0
    files_skipped   = 0
    rows_written = 0
    files_seen   = 0

    for path in paths:
        files_seen += 1
        m = _FILENAME_RE.match(path.name)
        if not m:
            files_skipped += 1
            continue
        accession = m.group("accession")
        if accession in seen_filings:
            files_skipped += 1
            continue

        meta = forms_map.get(accession)
        if meta is None:
            # No source_filing_state row — derive cik from filename, infer 10-K/10-Q later if possible.
            meta = {
                "cik":        m.group("cik"),
                "form":       "10-K/10-Q",  # placeholder; refined by SQL queries later
                "filed_date": None,
            }
        # Skip forms we know aren't 10-K/10-Q
        form = (meta.get("form") or "").upper()
        if form and form not in {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-K/10-Q"}:
            files_skipped += 1
            continue
        if meta.get("filed_date") is None:
            # Without a filed_date we can't satisfy NOT NULL constraint; skip.
            files_skipped += 1
            continue

        tuples = _extract_for_file(path, meta)
        if not tuples:
            files_skipped += 1
            continue

        batch.extend(tuples)
        files_with_data += 1

        if len(batch) >= 500:
            rows_written += _write_rows(batch)
            batch.clear()
            if files_seen % 1000 == 0:
                logger.info("processed %d files, %d with buybacks, %d rows so far",
                            files_seen, files_with_data, rows_written)

    rows_written += _write_rows(batch)

    return {
        "files_seen": files_seen,
        "files_with_data": files_with_data,
        "rows_written": rows_written,
        "files_skipped": files_skipped,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse Item 5 / Item 2(c) buybacks from US 10-K / 10-Q iXBRL HTML."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full",        action="store_true", help="Re-process every filing (default)")
    mode.add_argument("--incremental", action="store_true", help="Skip filings already in DB")
    parser.add_argument("--cik", type=str, default=None,
                        help="Restrict to a single CIK (e.g. 0000320193)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stats = ingest(cik_filter=args.cik, incremental=args.incremental)
    logger.info("done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
