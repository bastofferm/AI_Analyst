"""Extract historical shares-outstanding from JP EDINET XBRL filings.

Reads raw .xbrl files under market_data/japan_edinet/companyfacts/{EDINET}/ ,
locates jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults
elements, resolves their contextRef to the filing's period_end, normalises
by the XBRL `decimals` attribute, and UPSERTs into
sec.fact_fundamentals_std_jp with line_item_id='shares_outstanding'.

Filename pattern (per EDINET disclosure conventions):
    S1008JYI_jpcrp030000-asr-001_E00004-000_2016-05-31_01_2016-08-31.xbrl
    ^doc_id  ^form_code            ^entity  ^period   ^seq ^filed

CLI:
    python -m xbrl_sec.sec.sources.jp_shares_ingest --full
    python -m xbrl_sec.sec.sources.jp_shares_ingest --incremental
    python -m xbrl_sec.sec.sources.jp_shares_ingest --entity E00004
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator

import html as _html

from lxml import etree, html as lxml_html

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.settings import load_settings


logger = logging.getLogger(__name__)

# Annual ASR filings expose a structured numeric concept.
_SHARES_LOCAL_ASR = "TotalNumberOfIssuedSharesSummaryOfBusinessResults"

# Quarterly filings only carry the data inside an HTML <table> embedded in a
# text-block concept. Parse the HTML to extract numbers.
_SHARES_LOCAL_QUARTERLY = "IssuedSharesTotalNumberOfSharesEtcTextBlock"

# Source-concept ids (provenance) written into fact_fundamentals_std_jp.
_SOURCE_CONCEPT_ASR        = "jpcrp_cor/TotalNumberOfIssuedSharesSummaryOfBusinessResults"
_SOURCE_CONCEPT_QUARTERLY  = "jpcrp_cor/IssuedSharesTotalNumberOfSharesEtcTextBlock"

# Filename → metadata. Example:
#   S1008JYI_jpcrp030000-asr-001_E00004-000_2016-05-31_01_2016-08-31.xbrl
_FILENAME_RE = re.compile(
    r"^(?P<doc>[A-Z0-9]+)_"
    r"(?P<form_code>[a-z0-9]+-(?P<form>[a-z0-9]+)-\d+)_"
    r"(?P<entity>E\d{5})-(?P<seq>\d{3})_"
    r"(?P<period_end>\d{4}-\d{2}-\d{2})_"
    r"\d{2}_"
    r"(?P<filed>\d{4}-\d{2}-\d{2})"
    r"\.xbrl$"
)

# Map filing form code → fiscal_period label used in fact_fundamentals_std_jp.
_PERIOD_FROM_FORM = {
    "asr": "FY",   # annual securities report
    "q1r": "Q1",
    "q2r": "Q2",
    "q3r": "Q3",
    "ssr": "H1",   # semi-annual report (older filings)
    "srs": "H1",   # alternate code
}


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------

def _companyfacts_root() -> Path:
    return load_settings().project_root / "market_data" / "japan_edinet" / "companyfacts"


def _iter_xbrl_files(root: Path, entity_filter: str | None = None) -> Iterator[Path]:
    """Yield every .xbrl file under root[/{entity}]/ (skips _cal/_def/_lab sidecars)."""
    if entity_filter:
        sub = root / entity_filter
        if not sub.exists():
            return
        for p in sub.glob("*.xbrl"):
            yield p
    else:
        for entity_dir in sorted(root.iterdir()):
            if not entity_dir.is_dir():
                continue
            for p in entity_dir.glob("*.xbrl"):
                yield p


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_filename(path: Path) -> dict | None:
    m = _FILENAME_RE.match(path.name)
    if m is None:
        return None
    return {
        "doc_id":   m.group("doc"),
        "form":     m.group("form"),
        "form_code": m.group("form_code"),
        "entity_id": m.group("entity"),     # bare E00004 (matches dim_company_jp.edinet_code)
        "period_end": date.fromisoformat(m.group("period_end")),
        "filed_date": date.fromisoformat(m.group("filed")),
    }


def _build_context_map(root) -> dict[str, date]:
    """Map xbrli:context/@id → period_end (xbrli:instant) for every context in the doc."""
    ns = {"xbrli": "http://www.xbrl.org/2003/instance"}
    out: dict[str, date] = {}
    for ctx in root.iterfind(".//xbrli:context", ns):
        cid = ctx.get("id")
        inst = ctx.find("./xbrli:period/xbrli:instant", ns)
        if cid is None or inst is None or not (inst.text or "").strip():
            continue
        try:
            out[cid] = date.fromisoformat(inst.text.strip())
        except ValueError:
            continue
    return out


def _shares_value(elem) -> int | None:
    """Read structured <jpcrp_cor:TotalNumberOfIssuedShares...> element.

    Per XBRL 2.1, the `decimals` attribute describes precision (e.g. -3 = accurate
    to nearest thousand), not a scaling factor — the element's text is the actual
    share count, possibly rounded. No multiplier is applied.
    """
    raw = (elem.text or "").strip()
    if not raw:
        return None
    try:
        magnitude = float(raw)
    except ValueError:
        return None
    if magnitude <= 0:
        return None
    return int(magnitude)


# ---------------------------------------------------------------------------
# Quarterly HTML text-block parser
# ---------------------------------------------------------------------------

_TOTAL_LABEL_CHARS = "計"   # Japanese label for "total" row inside the table


def _shares_from_text_block(elem) -> int | None:
    """Extract the period-end share count from the HTML inside a
    jpcrp_cor:IssuedSharesTotalNumberOfSharesEtcTextBlock element.

    Quarterly filings carry an HTML <table> whose layout is:
        | 種類 | 期末発行数 | 提出日発行数 | 取引所 | 内容 |
        | 普通株式 | 11,772,626 | 11,772,626 | ... | ... |
        | 計      | 11,772,626 | 11,772,626 | - | - |

    Strategy:
      * Prefer the row whose first cell text contains 計 (total)
      * Else sum the period-end column across all data rows (multi-class issuers)
      * Take the second `<td>` (column index 1, zero-based) — period-end count
    """
    # The XBRL serialisation usually HTML-escapes the inner markup, but lxml
    # may have already given us actual child elements. Combine both paths.
    inner_text = (elem.text or "")
    inner_text += "".join(etree.tostring(c, encoding="unicode") for c in elem)
    if not inner_text.strip():
        return None

    # If escaped, unescape; lxml.html can handle either form.
    unescaped = _html.unescape(inner_text)

    try:
        root = lxml_html.fromstring(f"<root>{unescaped}</root>")
    except (etree.ParserError, etree.XMLSyntaxError):
        return None

    table = root.find(".//table")
    if table is None:
        return None

    body = table.find(".//tbody")
    rows = list((body if body is not None else table).findall(".//tr"))
    if not rows:
        return None

    def _cell_text(td) -> str:
        # Strip tags inside the cell, collapse whitespace.
        return " ".join(td.text_content().split()) if td is not None else ""

    def _cell_int(td) -> int | None:
        txt = _cell_text(td).replace(",", "").replace("，", "")  # full-width comma too
        if not txt:
            return None
        # Cells sometimes contain footnote markers like "*1"; strip non-digits.
        stripped = "".join(ch for ch in txt if ch.isdigit())
        if not stripped:
            return None
        try:
            v = int(stripped)
        except ValueError:
            return None
        return v if v > 0 else None

    total_row_value: int | None = None
    data_rows_sum: int = 0
    saw_any_data: bool = False

    for tr in rows:
        cells = tr.findall("./td")
        if len(cells) < 2:
            continue
        label = _cell_text(cells[0])
        val   = _cell_int(cells[1])
        if val is None:
            continue
        if _TOTAL_LABEL_CHARS in label:
            total_row_value = val
            # Don't break — sometimes the total row is followed by stray rows;
            # taking the LAST 計 row matches Japanese reporting conventions.
        else:
            data_rows_sum += val
            saw_any_data = True

    if total_row_value is not None:
        return total_row_value
    if saw_any_data:
        return data_rows_sum
    return None


def _find_by_local(root, local_name: str) -> list:
    """Return every element under `root` whose tag ends with `}local_name`."""
    return [
        e for e in root.iter()
        if isinstance(e.tag, str) and e.tag.endswith("}" + local_name)
    ]


# Cheap bytes-level pre-filter to avoid full XML parsing on files that don't
# carry the share-count concepts at all. ~10× speed-up on the full corpus.
_PRESCAN_TOKENS = (
    _SHARES_LOCAL_ASR.encode("ascii"),
    _SHARES_LOCAL_QUARTERLY.encode("ascii"),
)


def _extract_shares_for_file(path: Path) -> dict | None:
    """Return one canonical row for a single .xbrl file, or None.

    Dispatches by filing form:
      * `asr` (annual)          → structured `TotalNumberOfIssuedSharesSummaryOfBusinessResults`
      * `q1r/q2r/q3r/ssr/srs`   → HTML table inside `IssuedSharesTotalNumberOfSharesEtcTextBlock`
    """
    meta = _parse_filename(path)
    if meta is None:
        return None
    fiscal_period = _PERIOD_FROM_FORM.get(meta["form"])
    if fiscal_period is None:
        return None  # unknown form code → skip

    # Pre-filter: skip files that don't even mention either concept name.
    # Cheap (single read + bytes.find), avoids lxml/iterparse on ~half the corpus.
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        logger.debug("read error %s: %s", path.name, exc)
        return None
    if not any(tok in raw_bytes for tok in _PRESCAN_TOKENS):
        return None

    try:
        root = etree.fromstring(raw_bytes)
    except etree.XMLSyntaxError as exc:
        logger.debug("XML parse error in %s: %s", path.name, exc)
        return None

    target_period = meta["period_end"]
    best_value: int | None = None
    best_period: date | None = None
    source_concept: str | None = None

    # --- Path 1: structured ASR concept ------------------------------------
    asr_candidates = _find_by_local(root, _SHARES_LOCAL_ASR)
    if asr_candidates:
        ctx_map = _build_context_map(root)
        for elem in asr_candidates:
            ctx_ref = elem.get("contextRef") or ""
            ctx_period = ctx_map.get(ctx_ref)
            if ctx_period is None:
                continue
            val = _shares_value(elem)
            if val is None:
                continue
            # Exact match on filing period wins.
            if ctx_period == target_period:
                best_value, best_period = val, ctx_period
                source_concept = _SOURCE_CONCEPT_ASR
                break
            if best_period is None or ctx_period > best_period:
                best_value, best_period = val, ctx_period
                source_concept = _SOURCE_CONCEPT_ASR

    # --- Path 2: HTML text-block (quarterlies + fallback) ------------------
    if best_value is None:
        tb_candidates = _find_by_local(root, _SHARES_LOCAL_QUARTERLY)
        for elem in tb_candidates:
            val = _shares_from_text_block(elem)
            if val is None:
                continue
            # Text-block has no contextRef → key it to the filing's period_end.
            best_value = val
            best_period = target_period
            source_concept = _SOURCE_CONCEPT_QUARTERLY
            break

    if best_value is None or best_period is None or source_concept is None:
        return None

    return {
        "entity_id":      meta["entity_id"],
        "fiscal_year":    best_period.year,
        "fiscal_period":  fiscal_period,
        "period_end":     best_period,
        "value":          best_value,
        "filing_form":    meta["form"].upper(),
        "filed_date":     meta["filed_date"],
        "filing_id":      path.stem,
        "source_concept": source_concept,
    }


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO sec.fact_fundamentals_std_jp
    (edinet_code, jurisdiction, fiscal_year, fiscal_period, period_end,
     line_item_id, metric_type, value, currency,
     source_concept_id, filing_form, filed_date, filing_id,
     concept_path, std_concept_path)
VALUES %s
ON CONFLICT (edinet_code, jurisdiction, fiscal_year, fiscal_period, line_item_id)
DO UPDATE SET
    period_end          = EXCLUDED.period_end,
    value               = EXCLUDED.value,
    source_concept_id   = EXCLUDED.source_concept_id,
    filing_form         = EXCLUDED.filing_form,
    filed_date          = EXCLUDED.filed_date,
    filing_id           = EXCLUDED.filing_id,
    updated_at          = now()
"""

def _write_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    # Dedupe within the batch by PK (edinet_code, fiscal_year, fiscal_period);
    # multiple filings can cover the same period (amendments, re-statements) —
    # keep the most recently filed.
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["entity_id"], r["fiscal_year"], r["fiscal_period"])
        prev = best.get(key)
        if prev is None or r["filed_date"] > prev["filed_date"]:
            best[key] = r

    payload = [
        (
            r["entity_id"],
            "JP",
            r["fiscal_year"],
            r["fiscal_period"],
            r["period_end"],
            "shares_outstanding",
            "stock",                # snapshot, not flow
            r["value"],
            None,                   # currency
            r["source_concept"],
            r["filing_form"],
            r["filed_date"],
            r["filing_id"],
            r["source_concept"],    # concept_path
            "shares_outstanding",   # std_concept_path
        )
        for r in best.values()
    ]
    with connect() as conn, conn.cursor() as cur:
        execute_values(cur, _UPSERT_SQL, payload, page_size=500)
    return len(payload)


# ---------------------------------------------------------------------------
# Pipeline entry
# ---------------------------------------------------------------------------

def ingest(entity_filter: str | None = None, incremental: bool = False) -> dict:
    """Walk JP companyfacts, extract shares-outstanding, write to DB.

    Returns counts: {files_seen, rows_written, files_skipped}.
    """
    root = _companyfacts_root()
    if not root.exists():
        raise FileNotFoundError(f"JP companyfacts root not found: {root}")

    seen_filing_ids: set[str] = set()
    if incremental:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT filing_id FROM sec.fact_fundamentals_std_jp
                WHERE  line_item_id = 'shares_outstanding'
                  AND  filing_id IS NOT NULL
            """)
            seen_filing_ids = {r[0] for r in cur.fetchall()}
        logger.info("incremental mode: %d filings already ingested", len(seen_filing_ids))

    batch: list[dict] = []
    rows_written = 0
    files_seen = 0
    files_skipped = 0

    for path in _iter_xbrl_files(root, entity_filter):
        files_seen += 1
        if path.stem in seen_filing_ids:
            files_skipped += 1
            continue

        rec = _extract_shares_for_file(path)
        if rec is None:
            files_skipped += 1
            continue
        batch.append(rec)

        if len(batch) >= 500:
            rows_written += _write_rows(batch)
            batch.clear()
            if files_seen % 5000 == 0:
                logger.info("processed %d files, %d rows written so far", files_seen, rows_written)

    rows_written += _write_rows(batch)

    return {
        "files_seen":    files_seen,
        "rows_written":  rows_written,
        "files_skipped": files_skipped,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest JP shares-outstanding from EDINET XBRL.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full",        action="store_true", help="Re-process every file (default)")
    mode.add_argument("--incremental", action="store_true", help="Skip filings already in DB")
    parser.add_argument("--entity", type=str, default=None,
                        help="Restrict to a single EDINET code (e.g. E00004)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    stats = ingest(entity_filter=args.entity, incremental=args.incremental)
    logger.info("done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
