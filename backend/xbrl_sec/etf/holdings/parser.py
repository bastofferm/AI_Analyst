"""Provider-neutral parsing for official ETF holdings downloads."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Iterable

from .base import HoldingRow, HoldingsParseError


_NAME_KEYS = (
    "name", "holding", "holdingname", "security", "securityname", "description",
    "security description", "securitydescription", "assetname", "issuername",
    "company", "unternehmen", "inhaber",
)
_SYMBOL_KEYS = (
    "ticker", "symbol", "tickersymbol", "bloombergticker", "exchangeTicker",
    "emittententicker", "issuer ticker",
)
_ISIN_KEYS = ("isin", "holdingisin", "securityisin", "idisin")
_WEIGHT_KEYS = (
    "weight", "weight%", "weighting", "weighting%", "gewichtung%", "gewichten%",
    "portfolio%", "portfolio percent", "fund%", "%offund", "marketvalueweight",
    "percent of fund", "net assets", "net assets (%)", "net_assets", "net_assets%",
    "constituent weight", "constituent weight base", "constituent weight (base)",
)


def _clean_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9%]+", "", str(value or "").strip().lower())


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-"}:
        return None
    return s


def _first(row: dict[str, Any], keys: Iterable[str]) -> Any:
    normalized = {_clean_key(k): v for k, v in row.items()}
    for key in keys:
        if _clean_key(key) in normalized:
            return normalized[_clean_key(key)]
    return None


def _parse_weight(value: Any, header: str | None = None) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"nan", "none", "null", "-"}:
        return None
    is_percent = "%" in raw or (header is not None and "%" in header)
    raw = raw.replace("%", "").replace("\u00a0", "").replace(" ", "")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return None
    if number != number:
        return None
    if is_percent or number > 1.5:
        return number / 100.0
    return number


def rows_from_records(records: Iterable[dict[str, Any]]) -> list[HoldingRow]:
    rows: list[HoldingRow] = []
    for idx, row in enumerate(records, start=1):
        if not isinstance(row, dict):
            continue
        weight_header = next((k for k in row if _clean_key(k) in {_clean_key(x) for x in _WEIGHT_KEYS}), None)
        holding = HoldingRow(
            rank=idx,
            symbol=_text(_first(row, _SYMBOL_KEYS)),
            holding_isin=_text(_first(row, _ISIN_KEYS)),
            name=_text(_first(row, _NAME_KEYS)),
            weight=_parse_weight(_first(row, _WEIGHT_KEYS), weight_header),
        )
        if holding.name or holding.symbol or holding.holding_isin:
            rows.append(HoldingRow(
                rank=len(rows) + 1,
                symbol=holding.symbol,
                holding_isin=holding.holding_isin,
                name=holding.name,
                weight=holding.weight,
            ))
    return rows


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "latin-1"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text[:100]:
            return text
    return content.decode("utf-8", errors="replace")


def _candidate_delimiter(line: str) -> str:
    counts = {delim: line.count(delim) for delim in (",", ";", "\t", "|")}
    return max(counts, key=counts.get)


def _looks_like_header(cells: list[str]) -> bool:
    keys = {_clean_key(cell) for cell in cells}
    has_name = any(_clean_key(k) in keys for k in (*_NAME_KEYS, *_SYMBOL_KEYS, *_ISIN_KEYS))
    has_weight = any(_clean_key(k) in keys for k in _WEIGHT_KEYS)
    return has_name and has_weight


def parse_csv_holdings(content: bytes) -> list[HoldingRow]:
    text = _decode(content)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header_idx = None
    delimiter = ","
    for idx, line in enumerate(lines[:40]):
        delimiter = _candidate_delimiter(line)
        cells = next(csv.reader([line], delimiter=delimiter))
        if _looks_like_header(cells):
            header_idx = idx
            break
    if header_idx is None:
        raise HoldingsParseError("could not find holdings header row")
    reader = csv.DictReader(lines[header_idx:], delimiter=delimiter)
    return rows_from_records(reader)


def _find_json_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        rows = rows_from_records(value)
        if rows:
            return value
    if isinstance(value, dict):
        for key in ("holdings", "topHoldings", "constituents", "portfolio", "data"):
            if key in value:
                found = _find_json_records(value[key])
                if found:
                    return found
        for child in value.values():
            found = _find_json_records(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_json_records(child)
            if found:
                return found
    return []


def parse_json_holdings(content: bytes) -> list[HoldingRow]:
    try:
        data = json.loads(_decode(content))
    except json.JSONDecodeError as exc:
        raise HoldingsParseError(f"invalid JSON holdings payload: {exc}") from exc
    records = _find_json_records(data)
    return rows_from_records(records)


def parse_excel_holdings(content: bytes) -> list[HoldingRow]:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise HoldingsParseError("pandas/openpyxl is required for Excel holdings") from exc

    xls = pd.ExcelFile(io.BytesIO(content))
    for sheet in xls.sheet_names:
        raw = pd.read_excel(xls, sheet_name=sheet, header=None)
        for idx in range(min(len(raw), 40)):
            cells = [str(v) for v in raw.iloc[idx].tolist()]
            if _looks_like_header(cells):
                parsed = pd.read_excel(xls, sheet_name=sheet, header=idx)
                rows = rows_from_records(parsed.to_dict(orient="records"))
                if rows:
                    return rows
    return []


def parse_holdings(content: bytes, content_type: str | None = None, filename: str | None = None) -> list[HoldingRow]:
    marker = " ".join(v.lower() for v in (content_type or "", filename or ""))
    stripped = content.lstrip()
    if "json" in marker or stripped.startswith((b"{", b"[")):
        return parse_json_holdings(content)
    if any(ext in marker for ext in (".xlsx", ".xls", "spreadsheet", "excel")) or stripped.startswith(b"PK\x03\x04"):
        return parse_excel_holdings(content)
    return parse_csv_holdings(content)
