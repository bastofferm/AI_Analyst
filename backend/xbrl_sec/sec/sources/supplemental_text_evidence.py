"""Supplemental filing-text evidence for untagged strict XBRL components.

This module is a sidecar audit/evidence layer. It never writes to standardized
fundamentals and never changes governed concept mappings.
"""
from __future__ import annotations

import html
import json
import re
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.state.store import finish_run, start_run


_QUALITY_CODE = "aggregate_only_nonoperating_detail_missing"
_TARGET_LINE_ITEMS = ("interest_expense", "interest_income")

_LABEL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "interest_expense": (
        re.compile(r"\binterest\s+expense\b", re.I),
        re.compile(r"\binterest\s+costs?\s+incurred\b", re.I),
    ),
    "interest_income": (
        re.compile(r"\binterest\s+income\b", re.I),
        re.compile(r"\binterest\s+and\s+dividend\s+income\b", re.I),
        re.compile(r"\binvestment\s+income[, ]+\s*interest\s+and\s+dividend\b", re.I),
    ),
}

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TR_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.I | re.S)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.I | re.S)
_NUMBER_RE = re.compile(r"\(?\$?\s*-?\d[\d,]*(?:\.\d+)?\s*\)?")
_REJECT_ROW_MARKERS = (
    "basis point",
    "tenor",
    "sensitivity",
    "hypothetical",
    "increase in annual interest expense",
    "decrease in annual interest expense",
)


def _strip_tags(value: str) -> str:
    cleaned = _SCRIPT_STYLE_RE.sub(" ", value or "")
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _cell_texts(row_html: str) -> list[str]:
    cells = [_strip_tags(match.group(1)) for match in _CELL_RE.finditer(row_html)]
    return [cell for cell in cells if cell]


def _label_match(line_item_id: str, text: str) -> str | None:
    for pattern in _LABEL_PATTERNS.get(line_item_id, ()):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _scale_from_context(context: str) -> tuple[Decimal | None, str | None]:
    lowered = context.lower()
    if "in millions" in lowered or "dollars in millions" in lowered or "$ in millions" in lowered:
        return Decimal("1000000"), "USD"
    if "in thousands" in lowered or "dollars in thousands" in lowered or "$ in thousands" in lowered:
        return Decimal("1000"), "USD"
    if "in billions" in lowered or "dollars in billions" in lowered or "$ in billions" in lowered:
        return Decimal("1000000000"), "USD"
    return None, None


def _parse_number(text: str) -> Decimal | None:
    raw = text.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if negative:
        value = -value
    return value


def _candidate_numbers(cells: list[str], row_text: str) -> list[Decimal]:
    search_cells = cells[1:] if len(cells) > 1 else [row_text]
    out: list[Decimal] = []
    for cell in search_cells:
        for match in _NUMBER_RE.finditer(cell):
            value = _parse_number(match.group(0))
            if value is None:
                continue
            if value == value.to_integral_value() and Decimal("1900") <= value <= Decimal("2100"):
                continue
            out.append(value)
    return out


def _extract_from_html_text(
    text: str,
    source_path: str,
    line_item_id: str,
    fiscal_year: int,
) -> tuple[Decimal, str, str, str, Decimal] | None:
    best: tuple[Decimal, str, str, str, Decimal] | None = None
    for match in _TR_RE.finditer(text):
        row_html = match.group(0)
        row_text = _strip_tags(row_html)
        if not row_text or len(row_text) > 700:
            continue
        lowered_row = row_text.lower()
        if any(marker in lowered_row for marker in _REJECT_ROW_MARKERS):
            continue
        label = _label_match(line_item_id, row_text)
        if not label:
            continue
        cells = _cell_texts(row_html)
        if len(cells) < 2:
            continue
        context_start = max(0, match.start() - 5000)
        context = _strip_tags(text[context_start:match.end()])
        scale, currency = _scale_from_context(context)
        if scale is None:
            continue
        numbers = _candidate_numbers(cells, row_text)
        if not numbers:
            continue
        value = numbers[0] * scale
        if line_item_id == "interest_expense" and value > 0:
            value = -value
        confidence = Decimal("0.85") if len(cells) >= 3 else Decimal("0.75")
        excerpt = row_text[:1000]
        candidate = (value, currency or "USD", label, excerpt, confidence)
        if best is None or candidate[4] > best[4]:
            best = candidate
    return best


def _extract_from_mda_text(
    text: str,
    line_item_id: str,
) -> tuple[Decimal, str, str, str, Decimal] | None:
    # MDA text is plain text, so only accept short table-like lines with an
    # explicit scale cue. Broad prose is intentionally ignored.
    scale, currency = _scale_from_context(text[:5000])
    if scale is None:
        return None
    for line in re.split(r"[\r\n]+", text):
        row_text = re.sub(r"\s+", " ", line).strip()
        if not row_text or len(row_text) > 260:
            continue
        lowered_row = row_text.lower()
        if any(marker in lowered_row for marker in _REJECT_ROW_MARKERS):
            continue
        label = _label_match(line_item_id, row_text)
        if not label:
            continue
        numbers = _candidate_numbers([row_text], row_text)
        if numbers:
            value = numbers[0] * scale
            if line_item_id == "interest_expense" and value > 0:
                value = -value
            return (value, currency or "USD", label, row_text[:1000], Decimal("0.65"))
    return None


def _read_zip_html(path: str | None) -> list[tuple[str, str]]:
    if not path:
        return []
    zip_path = Path(path)
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        return []
    out: list[tuple[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = [
            name for name in zf.namelist()
            if name.lower().endswith((".htm", ".html"))
        ]
        for name in names:
            try:
                data = zf.read(name)
            except KeyError:
                continue
            text = data.decode("utf-8", errors="ignore")
            out.append((f"{zip_path}!{name}", text))
    return out


def _load_candidates(entity_ids: list[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND s.cik = ANY(%s)"
        params.append(entity_ids)
    limit_sql = ""
    if limit:
        limit_sql = "LIMIT %s"
        params.append(limit)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH period_items AS (
                SELECT s.cik, d.primary_ticker AS ticker, s.fiscal_year, s.fiscal_period,
                       s.period_end, s.filing_id,
                       BOOL_OR(s.line_item_id = 'non_operating_income') AS has_non_operating_income,
                       BOOL_OR(s.line_item_id = 'interest_expense') AS has_interest_expense,
                       BOOL_OR(s.line_item_id = 'interest_income') AS has_interest_income,
                       MAX(s.currency) FILTER (WHERE s.line_item_id = 'non_operating_income') AS currency
                FROM fact_fundamentals_std_us s
                JOIN dim_company_us d ON d.cik = s.cik
                WHERE COALESCE(d.mapping_sector, 'corp') = 'corp'
                  AND s.fiscal_period = 'FY'
                  AND s.filing_id IS NOT NULL
                  {entity_filter}
                GROUP BY s.cik, d.primary_ticker, s.fiscal_year, s.fiscal_period,
                         s.period_end, s.filing_id
            )
            SELECT p.cik, p.ticker, p.fiscal_year, p.fiscal_period, p.period_end,
                   p.filing_id, p.currency, p.has_interest_expense, p.has_interest_income,
                   fs.xbrl_package_path, fs.source_path
            FROM period_items p
            LEFT JOIN source_filing_state fs
              ON fs.jurisdiction = 'US'
             AND fs.entity_id = p.cik
             AND fs.filing_id = p.filing_id
            WHERE p.has_non_operating_income
              AND (NOT p.has_interest_expense OR NOT p.has_interest_income)
            ORDER BY p.cik, p.fiscal_year DESC
            {limit_sql}
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_mda_sections(cik: str, filing_id: str) -> list[tuple[str, str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT section_id, section_text
            FROM fact_mda_sections_us
            WHERE cik = %s AND filing_id = %s
            ORDER BY section_id
            """,
            (cik, filing_id),
        )
        return [(f"fact_mda_sections_us:{section_id}", text or "") for section_id, text in cur.fetchall()]


def _extract_evidence(row: dict[str, Any], line_item_id: str) -> tuple[Any, ...] | None:
    for source_path, text in _read_zip_html(row.get("xbrl_package_path")):
        extracted = _extract_from_html_text(text, source_path, line_item_id, int(row["fiscal_year"]))
        if extracted:
            value, currency, label, excerpt, confidence = extracted
            return (
                row["cik"], row["ticker"], row["filing_id"], row["fiscal_year"],
                row["fiscal_period"], row["period_end"], line_item_id, value,
                currency or row.get("currency") or "USD", label, excerpt,
                source_path, "html_table_regex", confidence,
            )
    for source_path, text in _load_mda_sections(str(row["cik"]), str(row["filing_id"])):
        extracted = _extract_from_mda_text(text, line_item_id)
        if extracted:
            value, currency, label, excerpt, confidence = extracted
            return (
                row["cik"], row["ticker"], row["filing_id"], row["fiscal_year"],
                row["fiscal_period"], row["period_end"], line_item_id, value,
                currency or row.get("currency") or "USD", label, excerpt,
                source_path, "mda_table_regex", confidence,
            )
    return None


def _populate_supplemental_metrics(cur, entity_ids: list[str] | None = None) -> int:
    entity_filter = ""
    params: list[Any] = []
    if entity_ids:
        entity_filter = "AND cik = ANY(%s)"
        params.append(entity_ids)
    cur.execute(
        f"""
        DELETE FROM fact_metrics_supplemental_us
        WHERE metric_id = 'interest_coverage_supplemental_text'
          {entity_filter}
        """,
        params,
    )
    insert_params: list[Any] = []
    entity_filter_insert = ""
    if entity_ids:
        entity_filter_insert = "AND e.cik = ANY(%s)"
        insert_params.append(entity_ids)
    cur.execute(
        f"""
        INSERT INTO fact_metrics_supplemental_us
            (cik, ticker, fiscal_year, fiscal_period, period_end, metric_id,
             value, unit_type, source_quality, source_line_item_id,
             source_filing_id, formula_with_values)
        SELECT e.cik,
               e.ticker,
               e.fiscal_year,
               e.fiscal_period,
               e.period_end,
               'interest_coverage_supplemental_text' AS metric_id,
               ebit.value / NULLIF(abs(e.value), 0) AS value,
               'x' AS unit_type,
               'SUPPLEMENTAL_TEXT' AS source_quality,
               e.line_item_id AS source_line_item_id,
               e.filing_id AS source_filing_id,
               'EBIT ' || ebit.value::text || ' / abs(text interest expense ' || e.value::text || ')' AS formula_with_values
        FROM fact_fundamentals_text_evidence_us e
        JOIN fact_fundamentals_std_us ebit
          ON ebit.cik = e.cik
         AND ebit.fiscal_year = e.fiscal_year
         AND ebit.fiscal_period = e.fiscal_period
         AND ebit.period_end = e.period_end
         AND ebit.line_item_id = 'earnings_before_interest_taxes'
        WHERE e.line_item_id = 'interest_expense'
          AND e.value IS NOT NULL
          AND e.value <> 0
          {entity_filter_insert}
        ON CONFLICT (cik, fiscal_year, fiscal_period, metric_id, source_quality)
        DO UPDATE SET
            ticker = EXCLUDED.ticker,
            period_end = EXCLUDED.period_end,
            value = EXCLUDED.value,
            unit_type = EXCLUDED.unit_type,
            source_line_item_id = EXCLUDED.source_line_item_id,
            source_filing_id = EXCLUDED.source_filing_id,
            formula_with_values = EXCLUDED.formula_with_values,
            updated_at = now()
        """,
        insert_params,
    )
    return max(cur.rowcount, 0)


def build_us_supplemental_text_evidence(
    entity_ids: list[str] | None = None,
    full: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Classify aggregate-only non-operating periods and extract strict text evidence."""
    ctx = start_run("US", "supplemental_text_evidence", "full_refresh" if full else "incremental")
    try:
        candidates = _load_candidates(entity_ids=entity_ids, limit=limit)
        quality_rows: list[tuple[Any, ...]] = []
        evidence_rows: list[tuple[Any, ...]] = []
        for row in candidates:
            missing = []
            if not row.get("has_interest_expense"):
                missing.append("interest_expense")
            if not row.get("has_interest_income"):
                missing.append("interest_income")
            for line_item_id in missing:
                details = {
                    "reason": "non_operating_income is tagged, but this component is not tagged in the same annual filing.",
                    "aggregate_line_item_id": "non_operating_income",
                    "strict_xbrl_only": True,
                }
                quality_rows.append((
                    row["cik"], row["ticker"], row["filing_id"], row["fiscal_year"],
                    row["fiscal_period"], row["period_end"], _QUALITY_CODE,
                    line_item_id, "WARN", "OPEN", json.dumps(details),
                ))
                evidence = _extract_evidence(row, line_item_id)
                if evidence:
                    evidence_rows.append(evidence)

        with connect() as conn, conn.cursor() as cur:
            if full and not entity_ids:
                cur.execute("DELETE FROM fact_fundamentals_quality_us WHERE quality_code = %s", (_QUALITY_CODE,))
                cur.execute(
                    "DELETE FROM fact_fundamentals_text_evidence_us WHERE line_item_id = ANY(%s)",
                    (list(_TARGET_LINE_ITEMS),),
                )
            elif entity_ids:
                cur.execute(
                    "DELETE FROM fact_fundamentals_quality_us WHERE quality_code = %s AND cik = ANY(%s)",
                    (_QUALITY_CODE, entity_ids),
                )
                cur.execute(
                    "DELETE FROM fact_fundamentals_text_evidence_us WHERE line_item_id = ANY(%s) AND cik = ANY(%s)",
                    (list(_TARGET_LINE_ITEMS), entity_ids),
                )

            quality_written = execute_values(
                cur,
                """
                INSERT INTO fact_fundamentals_quality_us
                    (cik, ticker, filing_id, fiscal_year, fiscal_period, period_end,
                     quality_code, line_item_id, severity, status, details)
                VALUES %s
                ON CONFLICT (cik, filing_id, fiscal_year, fiscal_period, quality_code, line_item_id)
                DO UPDATE SET
                    ticker = EXCLUDED.ticker,
                    period_end = EXCLUDED.period_end,
                    severity = EXCLUDED.severity,
                    status = EXCLUDED.status,
                    details = EXCLUDED.details,
                    updated_at = now()
                """,
                quality_rows,
                page_size=5000,
            )
            evidence_written = execute_values(
                cur,
                """
                INSERT INTO fact_fundamentals_text_evidence_us
                    (cik, ticker, filing_id, fiscal_year, fiscal_period, period_end,
                     line_item_id, value, currency, source_label, source_excerpt,
                     source_path, extraction_method, confidence)
                VALUES %s
                ON CONFLICT (
                    cik, filing_id, fiscal_year, fiscal_period,
                    line_item_id, extraction_method, source_label
                )
                DO UPDATE SET
                    ticker = EXCLUDED.ticker,
                    period_end = EXCLUDED.period_end,
                    value = EXCLUDED.value,
                    currency = EXCLUDED.currency,
                    source_excerpt = EXCLUDED.source_excerpt,
                    source_path = EXCLUDED.source_path,
                    confidence = EXCLUDED.confidence,
                    updated_at = now()
                """,
                evidence_rows,
                page_size=1000,
            )
            supplemental_metrics_written = _populate_supplemental_metrics(cur, entity_ids)

        counts = {
            "candidate_periods": len(candidates),
            "quality_rows": quality_written,
            "text_evidence_rows": evidence_written,
            "supplemental_metric_rows": supplemental_metrics_written,
            "missing_interest_expense": sum(1 for r in quality_rows if r[7] == "interest_expense"),
            "missing_interest_income": sum(1 for r in quality_rows if r[7] == "interest_income"),
        }
        finish_run(ctx, "succeeded", rows_in=len(candidates), rows_out=quality_written + evidence_written)
        return counts
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise


def supplemental_text_evidence_summary_json(limit: int = 50) -> str:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.ticker, q.cik, q.fiscal_year, q.filing_id, q.line_item_id,
                   q.quality_code, q.status,
                   e.value, e.currency, e.extraction_method, e.confidence,
                   e.source_label
            FROM fact_fundamentals_quality_us q
            LEFT JOIN fact_fundamentals_text_evidence_us e
              ON e.cik = q.cik
             AND e.filing_id = q.filing_id
             AND e.fiscal_year = q.fiscal_year
             AND e.fiscal_period = q.fiscal_period
             AND e.line_item_id = q.line_item_id
            WHERE q.quality_code = %s
            ORDER BY q.ticker, q.fiscal_year DESC, q.line_item_id
            LIMIT %s
            """,
            (_QUALITY_CODE, limit),
        )
        cols = [desc[0] for desc in cur.description]
        return json.dumps([dict(zip(cols, row)) for row in cur.fetchall()], default=str, indent=2)
