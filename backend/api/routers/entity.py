from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path as FsPath
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Query

from ..db import acquire
from ..models.company import EntityIdentity


router = APIRouter()


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_fy_end(s: str | None) -> str | None:
    """Parse fiscal_year_end values like 'MMDD' or 'MM-DD' into 'Mon-DD' display."""
    if not s:
        return None
    digits = "".join(ch for ch in s.strip() if ch.isdigit())
    if len(digits) == 8:
        digits = digits[4:]
    if len(digits) != 4:
        return None
    m, d = int(digits[:2]), int(digits[2:])
    if 1 <= m <= 12 and 1 <= d <= 31:
        return f"{_MONTHS[m - 1]}-{d:02d}"
    return None


_PERIOD_CODES = {"FY", "Annual", "H1", "H2", "Q1", "Q2", "Q3", "Q4"}
_PROJECT_ROOT = FsPath(__file__).resolve().parents[2]
_DESC_ROOT = _PROJECT_ROOT / "company_metadata" / "descriptions" / "yahoo_finance"
_DESC_CSV = _DESC_ROOT / "yahoo_descriptions.csv"
_DESC_FALLBACK = "No compact company description available."


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _read_description_file(path: FsPath | None) -> str:
    if not path or not path.exists():
        return ""
    try:
        return _clean_text(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return _clean_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


@lru_cache(maxsize=1)
def _description_index() -> dict[tuple[str, str, str], dict[str, str]]:
    if not _DESC_CSV.exists():
        return {}
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    try:
        with _DESC_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                jurisdiction = _clean_text(row.get("jurisdiction")).upper()
                ticker = _clean_text(row.get("ticker")).upper()
                company_id = _clean_text(row.get("company_id"))
                if not jurisdiction or not ticker or not company_id:
                    continue
                out[(jurisdiction, ticker, company_id)] = {
                    "compact_description": _clean_text(row.get("compact_description")),
                    "path": _clean_text(row.get("path")),
                }
    except OSError:
        return {}
    return out


def _fallback_description_path(ticker: str, company_id: str, jurisdiction: str) -> FsPath | None:
    if not ticker or not company_id:
        return None
    if jurisdiction == "US":
        return _DESC_ROOT / "us" / f"US_{ticker.upper()}_{company_id}.compact.txt"
    if jurisdiction == "JP":
        return _DESC_ROOT / "jp" / f"JP_{ticker}_{company_id}.compact.txt"
    return None


def _lookup_compact_description(ticker: str, company_id: str | None, jurisdiction: str) -> dict[str, str | None]:
    company_id = company_id or ""
    record = _description_index().get((jurisdiction, ticker.upper(), company_id))
    path_text = (record or {}).get("path") or ""
    path = FsPath(path_text) if path_text else None
    if path and not path.is_absolute():
        path = _PROJECT_ROOT / path
    description = _read_description_file(path)
    if not description:
        description = _clean_text((record or {}).get("compact_description"))
    fallback_path = _fallback_description_path(ticker, company_id, jurisdiction)
    if not description and fallback_path:
        description = _read_description_file(fallback_path)
        path = fallback_path if description else path
    if not description:
        return {"description_compact": _DESC_FALLBACK, "description_source": None, "description_path": None}
    return {
        "description_compact": description,
        "description_source": "yahoo_finance_compact",
        "description_path": str(path) if path else path_text or None,
    }


async def _filing_profile(conn, ticker: str, jurisdiction: str) -> list[str]:
    """Distinct filing forms for the entity (10-K, 10-Q, …). Ports _filing_profile from mzqa_terminal_v2.py."""
    if jurisdiction == "US":
        sql = """
            SELECT DISTINCT COALESCE(NULLIF(s.filing_form, ''), NULLIF(s.fiscal_period, '')) AS filing
            FROM   fact_fundamentals_std_us s
            JOIN   dim_company_us d ON d.cik = s.cik
            WHERE  d.primary_ticker = $1
              AND  COALESCE(NULLIF(s.filing_form, ''), NULLIF(s.fiscal_period, '')) IS NOT NULL
            ORDER  BY 1
        """
    else:
        sql = """
            SELECT DISTINCT COALESCE(NULLIF(s.filing_form, ''), NULLIF(s.fiscal_period, '')) AS filing
            FROM   fact_fundamentals_std_jp s
            JOIN   dim_company_jp d ON d.edinet_code = s.edinet_code
            WHERE  d.primary_ticker = $1
              AND  COALESCE(NULLIF(s.filing_form, ''), NULLIF(s.fiscal_period, '')) IS NOT NULL
            ORDER  BY 1
        """
    rows = await conn.fetch(sql, ticker)
    forms = [str(r["filing"]) for r in rows if r["filing"] and str(r["filing"]) not in _PERIOD_CODES]
    return forms


@router.get("/{ticker}", response_model=EntityIdentity)
async def get_entity(
    ticker: str = Path(...),
    jurisdiction: Literal["US", "JP"] = Query(...),
) -> EntityIdentity:
    if jurisdiction == "US":
        sql = """
            SELECT primary_ticker        AS ticker,
                   name,
                   cik::text             AS cik,
                   exchange,
                   sic,
                   sic_description,
                   fiscal_year_end,
                   gics_sector_name,
                   gics_industry_group_name
            FROM   dim_company_us
            WHERE  primary_ticker = $1
            LIMIT  1
        """
    else:
        sql = """
            SELECT primary_ticker                          AS ticker,
                   COALESCE(name_en, name, primary_ticker) AS name,
                   edinet_code,
                   fiscal_year_end,
                   gics_sector_name,
                   gics_industry_group_name,
                   COALESCE(
                       NULLIF(gics_industry_group_name, ''),
                       NULLIF(gics_sector_name, '')
                   ) AS gics_display_name
            FROM   dim_company_jp
            WHERE  primary_ticker = $1
            LIMIT  1
        """

    async with acquire() as conn:
        row = await conn.fetchrow(sql, ticker)
        if row is None:
            raise HTTPException(status_code=404, detail=f"ticker not found: {ticker}")

        filings = await _filing_profile(conn, ticker, jurisdiction)
        jp_fy_end = None
        if jurisdiction == "JP" and not row["fiscal_year_end"]:
            fy_row = await conn.fetchrow(
                """
                SELECT to_char(period_end, 'MMDD') AS fiscal_year_end
                FROM fact_fundamentals_std_jp
                WHERE edinet_code = $1
                  AND fiscal_period IN ('FY', 'Annual')
                  AND period_end IS NOT NULL
                GROUP BY 1
                ORDER BY COUNT(*) DESC, MAX(fiscal_year) DESC
                LIMIT 1
                """,
                row["edinet_code"],
            )
            jp_fy_end = fy_row["fiscal_year_end"] if fy_row and fy_row["fiscal_year_end"] else None

    if jurisdiction == "US":
        description = _lookup_compact_description(row["ticker"], row["cik"], "US")
        return EntityIdentity(
            ticker=row["ticker"],
            name=row["name"] or row["ticker"],
            jurisdiction="US",
            cik=row["cik"],
            edinet_code=None,
            exchange=row["exchange"],
            sic_code=row["sic"],
            fy_end=_fmt_fy_end(row["fiscal_year_end"]),
            gics_sector_name=row["gics_sector_name"],
            gics_industry_group_name=row["gics_industry_group_name"],
            filings=filings or ["10-K"],
            **description,
        )

    description = _lookup_compact_description(row["ticker"], row["edinet_code"], "JP")
    return EntityIdentity(
        ticker=row["ticker"],
        name=row["name"] or row["ticker"],
        jurisdiction="JP",
        cik=None,
        edinet_code=row["edinet_code"],
        exchange=None,
        sic_code=row["gics_display_name"],
        fy_end=_fmt_fy_end(row["fiscal_year_end"] or jp_fy_end),
        gics_sector_name=row["gics_sector_name"],
        gics_industry_group_name=row["gics_industry_group_name"],
        filings=filings or ["有価証券報告書"],
        **description,
    )
