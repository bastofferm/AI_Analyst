"""Off-income-statement segment extractor.

The standardized fact tables carry only *consolidated* figures — US fundamentals
come from the SEC companyfacts API, which has no dimensional/segment breakdown.
Reportable-segment revenue & profitability, product/geographic splits and the
management growth/challenge narrative therefore have to be read out of the raw
``xbrl_html`` filing note tables on ``D:\\market_data\\us_sec\\xbrl_html`` — the
same files the MD&A pipeline consumes.

US path : locate the latest (and prior-year) 10-K via ``source_filing_state``,
          resolve the html, pull the Segment-Reporting disclosure textblock, parse
          its embedded table best-effort, and keep the cleaned text as narrative.
JP path : dimensional segment facts already survive in ``fact_fundamentals_jp``
          via ``dimension_signature`` — read them directly, no HTML parse.

Everything degrades gracefully: on any failure the result carries
``available=False`` and an empty ``structured`` list, and the tribunal simply
argues from the consolidated packet.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .._db import read_sql
from xbrl_sec.sec.mda.settings import html_dir_from_env
from xbrl_sec.sec.mda.text import clean_html_to_text, soup_from_html


# iXBRL textblock concepts (local-part, lowercased) that hold the segment note.
_SEGMENT_TEXTBLOCK_CONCEPTS = (
    "segmentreportingdisclosuretextblock",
    "scheduleofsegmentreportinginformationbysegmenttextblock",
    "scheduleofsegmentreportinginformationbysegmentandgeographicareatextblock",
)

# XBRL dimension axes (local-part, lowercased) and the concepts we read per member.
_BUSINESS_SEGMENT_AXES = ("statementbusinesssegmentsaxis",)
_PRODUCT_AXES = ("productorserviceaxis",)
_GEO_AXES = ("statementgeographicalaxis",)
_REVENUE_CONCEPTS = frozenset({
    "revenuefromcontractwithcustomerexcludingassessedtax",
    "revenuefromcontractwithcustomerincludingassessedtax",
    "revenuefromexternalcustomers",
    "revenues", "revenue", "salesrevenuenet", "salesrevenuegoodsnet",
})
_OPINCOME_CONCEPTS = frozenset({
    "operatingincomeloss",
    "grossprofit",
    # Some issuers, including QCOM, report segment profitability as pre-tax
    # segment earnings on StatementBusinessSegmentsAxis rather than op income.
    "incomelossfromcontinuingoperationsbeforeincometaxesextraordinaryitemsnoncontrollinginterest",
})
_SEGMENT_HEADING_RE = re.compile(
    r"(?is)(segment\s+information|reportable\s+segments?|business\s+segments?|"
    r"operating\s+segments?|segment\s+reporting)"
)
_REVENUE_HINT = re.compile(r"(?i)revenue|net\s+sales|sales|turnover")
_OPINCOME_HINT = re.compile(r"(?i)operating\s+(income|profit)|segment\s+(profit|income)|"
                            r"income\s+from\s+operations|ebit")
_NARRATIVE_CAP = 8000


def extract_segments(cik: str | None, jurisdiction: str, target_years: list[int] | None = None) -> dict[str, Any]:
    """Return off-statement segment disclosure for one entity.

    Shape: ``{"structured": [...], "product_geo": [...], "narrative": str,
    "source": accession, "available": bool, "note": str}``.
    """
    try:
        if jurisdiction == "JP":
            return _extract_jp(cik, target_years)
        return _extract_us(cik)
    except Exception as exc:  # noqa: BLE001 - never break the pipeline on extraction
        return _empty(note=f"segment extraction failed: {exc.__class__.__name__}: {exc}"[:300])


# --------------------------------------------------------------------------- US

def _extract_us(cik: str | None) -> dict[str, Any]:
    if not cik:
        return _empty(note="no CIK for US entity")
    cik10 = str(cik).zfill(10)
    filings = read_sql(
        """
        SELECT filing_id, filed_date, period_end
        FROM source_filing_state
        WHERE jurisdiction = 'US'
          AND entity_id = %(cik)s
          AND filing_type IN ('10-K','10-K/A')
          AND COALESCE(parsed, FALSE)
        ORDER BY filed_date DESC NULLS LAST
        LIMIT 3
        """,
        {"cik": cik10},
    )
    if filings.empty:
        return _empty(note="no parsed 10-K in source_filing_state")

    html_dir = html_dir_from_env()
    for _, row in filings.iterrows():
        filing_id = str(row["filing_id"])
        path = html_dir / f"CIK{cik10}_{filing_id}.htm"
        if not path.exists():
            continue
        html = _read_html_file(path)
        if not html:
            continue
        soup = soup_from_html(html)
        all_tags = soup.find_all(True)  # single traversal, reused everywhere below

        # Primary, reliable source: the tagged iXBRL facts on the segment axes.
        structured, product_geo = _segments_from_ixbrl(all_tags)

        inner = _segment_textblock_html_from_tags(all_tags)
        narrative = clean_html_to_text(inner)[:_NARRATIVE_CAP] if inner else (_fallback_narrative(html) or "")

        # Fallback if no tagged segment facts: best-effort HTML-table parse of the note.
        note = "segment facts from tagged iXBRL (StatementBusinessSegmentsAxis)"
        if not structured and inner:
            structured = _parse_segment_tables(inner, want=_REVENUE_HINT)
            note = "segment rows from best-effort HTML-table parse"

        if structured or narrative:
            return {
                "structured": structured[:20],
                "product_geo": [r for r in product_geo if r not in structured][:20],
                "narrative": narrative,
                "source": filing_id,
                "period_end": _iso(row.get("period_end")),
                "available": bool(structured or narrative),
                "note": note,
            }
    return _empty(note="10-K html not found on disk or no segment note present")


def extract_segment_trend(cik: str | None, jurisdiction: str, max_years: int = 4) -> dict[str, list[dict[str, Any]]]:
    """Multi-year segment revenue/margin series from the latest 10-K note (~3 yrs tagged).

    Returns ``{segment_name: [{fiscal_year, revenue, operating_income, operating_margin}, ...]}``.
    US-only (iXBRL); returns {} otherwise or on any failure.
    """
    if jurisdiction != "US" or not cik:
        return {}
    try:
        cik10 = str(cik).zfill(10)
        filings = read_sql(
            """
            SELECT filing_id FROM source_filing_state
            WHERE jurisdiction='US' AND entity_id=%(c)s AND filing_type IN ('10-K','10-K/A')
              AND COALESCE(parsed,FALSE) ORDER BY filed_date DESC NULLS LAST LIMIT 2
            """,
            {"c": cik10},
        )
        html_dir = html_dir_from_env()
        for _, row in filings.iterrows():
            path = html_dir / f"CIK{cik10}_{row['filing_id']}.htm"
            if not path.exists():
                continue
            html = _read_html_file(path)
            if not html:
                continue
            all_tags = soup_from_html(html).find_all(True)
            series = _segment_series_from_tags(all_tags, max_years)
            if series:
                return series
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _segment_series_from_tags(all_tags, max_years: int) -> dict[str, list[dict[str, Any]]]:
    contexts = _parse_contexts(all_tags)
    if not contexts:
        return {}
    acc: dict[str, dict[int, dict[str, float]]] = {}  # member -> year -> {revenue, operating_income}
    for tag in all_tags:
        concept = str(tag.get("name") or "")
        if not concept:
            continue
        local = concept.split(":")[-1].lower()
        kind = "revenue" if local in _REVENUE_CONCEPTS else ("operating_income" if local in _OPINCOME_CONCEPTS else None)
        if kind is None:
            continue
        ctx = contexts.get(str(tag.get("contextref") or ""))
        if not ctx:
            continue
        member = ctx["axes"].get("business")
        if not member or _is_noise_member(member):
            continue
        end = ctx.get("end")
        if not (end and len(end) >= 4 and end[:4].isdigit()):
            continue
        year = int(end[:4])
        value = _ixbrl_value(tag)
        if value is None:
            continue
        acc.setdefault(member, {}).setdefault(year, {})[kind] = value
    out: dict[str, list[dict[str, Any]]] = {}
    for member, years in acc.items():
        rows = []
        for yr in sorted(years)[-max_years:]:
            vv = years[yr]
            rev, oi = vv.get("revenue"), vv.get("operating_income")
            if rev is None and oi is None:
                continue
            rows.append({"fiscal_year": yr, "revenue": rev, "operating_income": oi,
                         "operating_margin": (oi / rev) if (oi is not None and rev) else None})
        if len(rows) >= 2:
            out[_humanize_member(member)] = rows
    return out


def _segments_from_ixbrl(all_tags) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read segment revenue & operating income from tagged iXBRL facts.

    One pass builds contextRef -> members per axis; one pass over facts aggregates
    the revenue/op-income concepts per member (latest period wins). Business-segment
    members feed ``structured``; product/geographic members feed ``product_geo``.
    Returns ``(business_rows, product_geo_rows)``.
    """
    contexts = _parse_contexts(all_tags)
    if not contexts:
        return [], []
    biz: dict[str, dict[str, Any]] = {}
    pg: dict[str, dict[str, Any]] = {}
    for tag in all_tags:
        concept = str(tag.get("name") or "")
        if not concept:
            continue
        local = concept.split(":")[-1].lower()
        kind = "revenue" if local in _REVENUE_CONCEPTS else ("operating_income" if local in _OPINCOME_CONCEPTS else None)
        if kind is None:
            continue
        ctx = contexts.get(str(tag.get("contextref") or ""))
        if not ctx:
            continue
        member = ctx["axes"].get("business") or ctx["axes"].get("product") or ctx["axes"].get("geo")
        if not member or _is_noise_member(member):
            continue
        value = _ixbrl_value(tag)
        if value is None:
            continue
        acc = biz if ctx["axes"].get("business") else pg
        end = ctx["end"]
        slot = acc.setdefault(member, {"segment": _humanize_member(member), "raw_member": member, "periods": {}})
        period_slot = slot["periods"].setdefault(end or "", {})
        prev = period_slot.get(kind)
        candidate = (_segment_fact_priority(local, kind), value)
        if prev is None or candidate[0] >= prev[0]:
            period_slot[kind] = candidate

    return _acc_to_rows(biz), _acc_to_rows(pg)


def _acc_to_rows(acc: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slot in acc.values():
        periods = slot.get("periods") or {}
        for period_end, bucket in sorted(periods.items(), key=lambda item: item[0] or "", reverse=True):
            rev = bucket.get("revenue")
            oi = bucket.get("operating_income")
            revenue = rev[1] if rev else None
            op_income = oi[1] if oi else None
            if revenue is None and op_income is None:
                continue
            margin = (op_income / revenue) if (op_income is not None and revenue) else None
            rows.append({
                "segment": slot["segment"],
                "revenue": revenue,
                "operating_income": op_income,
                "operating_margin": round(margin, 4) if margin is not None else None,
                "period_end": period_end or None,
            })
            break
    rows.sort(key=lambda r: (r.get("revenue") is None, -(r.get("revenue") or 0)))
    return rows


def _segment_fact_priority(local_concept: str, kind: str) -> int:
    if kind == "revenue":
        if local_concept.startswith("revenuefromcontractwithcustomer"):
            return 3
        if local_concept == "revenuefromexternalcustomers":
            return 2
        return 1
    if local_concept == "operatingincomeloss":
        return 3
    return 2


def _parse_contexts(all_tags) -> dict[str, dict[str, Any]]:
    """Map contextRef id -> {"axes": {business/product/geo: member}, "end": date}."""
    out: dict[str, dict[str, Any]] = {}
    for tag in all_tags:
        if not (tag.name or "").endswith("context") or not tag.get("id"):
            continue
        axes: dict[str, str] = {}
        for em in tag.find_all(True):
            nm = em.name or ""
            if nm.endswith("explicitmember"):
                dim = str(em.get("dimension") or "").split(":")[-1].lower()
                group = ("business" if dim in _BUSINESS_SEGMENT_AXES
                         else "product" if dim in _PRODUCT_AXES
                         else "geo" if dim in _GEO_AXES else None)
                if group and group not in axes:
                    axes[group] = (em.get_text() or "").strip()
        if not axes:
            continue
        end = None
        for child in tag.find_all(True):
            cn = child.name or ""
            if cn.endswith("enddate") or cn.endswith("instant"):
                end = (child.get_text() or "").strip()
        out[str(tag.get("id"))] = {"axes": axes, "end": end}
    return out


def _ixbrl_value(tag) -> float | None:
    text = (tag.get_text() or "").strip()
    if not text or text == "-":
        return None
    neg = text.startswith("(") and text.endswith(")")
    s = text.replace(",", "").replace("(", "").replace(")", "").replace("$", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    scale = tag.get("scale")
    if scale not in (None, ""):
        try:
            v *= 10 ** int(scale)
        except ValueError:
            pass
    if neg or str(tag.get("sign") or "") == "-":
        v = -abs(v)
    return v


_NOISE_MEMBER_RE = re.compile(r"(?i)aggregat|intersegment|elimination|reconcil|corporatenonsegment")


def _is_noise_member(member: str) -> bool:
    return bool(_NOISE_MEMBER_RE.search(member.split(":")[-1]))


def _humanize_member(member: str) -> str:
    local = member.split(":")[-1]
    local = re.sub(r"Member$", "", local)
    local = re.sub(r"Segment$", "", local)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", local)
    return spaced.strip()[:120] or member[:120]


def _segment_textblock_html_from_tags(all_tags) -> str | None:
    """Return the inner HTML of the segment-reporting iXBRL textblock (largest match)."""
    matches: list[str] = []
    for tag in all_tags:
        name = str(tag.get("name") or "")
        local = name.split(":")[-1].lower()
        if local in _SEGMENT_TEXTBLOCK_CONCEPTS:
            raw = tag.decode_contents()
            if raw:
                matches.append(raw)
    if not matches:
        return None
    return max(matches, key=len)


def _fallback_narrative(html: str) -> str | None:
    text = clean_html_to_text(html)
    if not text:
        return None
    match = _SEGMENT_HEADING_RE.search(text)
    if not match:
        return None
    start = match.start()
    return text[start:start + _NARRATIVE_CAP].strip() or None


def _parse_segment_tables(inner_html: str, want: re.Pattern) -> list[dict[str, Any]]:
    """Best-effort parse of the segment note's HTML tables into structured rows.

    Picks the table whose header/cells look like a segment revenue (or split) table
    and maps: first text column -> segment label, revenue-ish column -> revenue,
    operating-income-ish column -> operating_income.
    """
    try:
        tables = pd.read_html(io.StringIO(inner_html))
    except Exception:  # noqa: BLE001 - malformed note tables are common
        return []
    best: list[dict[str, Any]] = []
    for tbl in tables:
        rows = _table_to_rows(tbl, want)
        if len(rows) > len(best):
            best = rows
    return best[:20]


def _table_to_rows(tbl: pd.DataFrame, want: re.Pattern) -> list[dict[str, Any]]:
    if tbl is None or tbl.empty or tbl.shape[1] < 2:
        return []
    tbl = tbl.dropna(how="all").dropna(axis=1, how="all")
    if tbl.shape[1] < 2:
        return []
    header_blob = " ".join(str(c) for c in tbl.columns) + " " + " ".join(
        str(v) for v in tbl.iloc[0].tolist()
    )
    if not want.search(header_blob):
        return []
    label_col = tbl.columns[0]
    rev_col = _pick_numeric_col(tbl, _REVENUE_HINT)
    oi_col = _pick_numeric_col(tbl, _OPINCOME_HINT)
    rows: list[dict[str, Any]] = []
    for _, r in tbl.iterrows():
        label = str(r.get(label_col, "")).strip()
        if not label or label.lower() in {"nan", "total", "consolidated", "eliminations"}:
            continue
        revenue = _num(r.get(rev_col)) if rev_col is not None else None
        oi = _num(r.get(oi_col)) if oi_col is not None else None
        if revenue is None and oi is None:
            continue
        margin = (oi / revenue) if (oi is not None and revenue) else None
        rows.append({
            "segment": label[:120],
            "revenue": revenue,
            "operating_income": oi,
            "operating_margin": round(margin, 4) if margin is not None else None,
        })
    return rows


def _pick_numeric_col(tbl: pd.DataFrame, hint: re.Pattern) -> Any:
    # Prefer a column whose header matches the hint; else the first mostly-numeric column.
    for col in tbl.columns:
        if hint.search(str(col)):
            return col
    for col in tbl.columns[1:]:
        vals = [_num(v) for v in tbl[col].tolist()]
        if sum(v is not None for v in vals) >= max(2, len(vals) // 2):
            return col
    return None


# --------------------------------------------------------------------------- JP

def _extract_jp(edinet_or_cik: str | None, target_years: list[int] | None) -> dict[str, Any]:
    if not edinet_or_cik:
        return _empty(note="no EDINET code for JP entity")
    year_filter = ""
    params: dict[str, Any] = {"eid": edinet_or_cik}
    if target_years:
        year_filter = "AND fiscal_year = ANY(%(years)s)"
        params["years"] = list(target_years)
    df = read_sql(
        f"""
        SELECT fiscal_year, dimension_signature, line_item_id,
               value::double precision AS value, currency
        FROM fact_fundamentals_jp
        WHERE edinet_code = %(eid)s
          AND dimension_signature <> ''
          AND dimension_signature ILIKE '%%segment%%'
          {year_filter}
        ORDER BY fiscal_year DESC, dimension_signature
        LIMIT 400
        """,
        params,
    )
    if df.empty:
        return _empty(note="no dimensional segment facts in fact_fundamentals_jp")
    structured: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        structured.append({
            "segment": str(row["dimension_signature"])[:120],
            "fiscal_year": int(row["fiscal_year"]) if row["fiscal_year"] is not None else None,
            "line_item_id": row["line_item_id"],
            "value": row["value"],
            "currency": row["currency"],
        })
    return {
        "structured": structured,
        "product_geo": [],
        "narrative": "",
        "source": "fact_fundamentals_jp",
        "available": True,
        "note": "JP dimensional segment facts",
    }


# ------------------------------------------------------------------------ utils

def _read_html_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("(", "").replace(")", "").replace(",", "").replace("$", "").replace("¥", "").strip()
    s = s.replace("%", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _empty(note: str = "") -> dict[str, Any]:
    return {
        "structured": [],
        "product_geo": [],
        "narrative": "",
        "source": None,
        "available": False,
        "note": note,
    }
