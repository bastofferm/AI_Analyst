"""EDINET document type policy for the JP fundamentals pipeline."""
from __future__ import annotations

from collections.abc import Iterable


DOC_TYPE_CODES = frozenset(
    {
        "010",
        "020",
        "030",
        "040",
        "050",
        "120",
        "130",
        "140",
        "150",
        "160",
        "170",
    }
)


def normalize_doc_type_code(value: object) -> str:
    return str(value or "").strip().zfill(3)


def normalize_doc_type_codes(filing_types: Iterable[object] | None) -> tuple[str, ...] | None:
    if filing_types is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for value in filing_types:
        code = normalize_doc_type_code(value)
        if code not in DOC_TYPE_CODES:
            raise ValueError(f"Unsupported JP EDINET document type {value!r}; choose from {', '.join(sorted(DOC_TYPE_CODES))}")
        if code not in seen:
            result.append(code)
            seen.add(code)
    if not result:
        raise ValueError("At least one JP EDINET document type must be selected")
    return tuple(result)
