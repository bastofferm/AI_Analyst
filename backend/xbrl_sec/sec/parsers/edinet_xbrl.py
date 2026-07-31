"""EDINET XBRL parser for sec.fact_fundamentals_jp."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from xbrl_sec.sec.sources.edinet_filings import EdinetXbrlFile
from xbrl_sec.sec.writers.raw_facts import upsert_jp_facts


_XBRLI = "http://www.xbrl.org/2003/instance"
_XBRLDI = "http://xbrl.org/2006/xbrldi"
_NON_NUMERIC_SUFFIXES = (
    "TextBlock",
    "Abstract",
    "Axis",
    "Domain",
    "Member",
    "LineItems",
    "Table",
    "RollForward",
)
_INTERIM_DOC_TYPES = {"140", "150", "160", "170"}
_AMENDMENT_DOC_TYPES = {"130", "150", "170"}
_IDENTITY_CONCEPTS = {
    "jpdei_cor/FilerNameInEnglishDEI": "name_en",
    "jpcrp_cor/CompanyNameInEnglishCoverPage": "name_en_cover",
    "jpdei_cor/FilerNameInJapaneseDEI": "name",
    "jpcrp_cor/CompanyNameCoverPage": "name_cover",
    "jpdei_cor/SecurityCodeDEI": "sec_code",
}
_LINK_NS = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_XLINK_HREF = f"{{{_XLINK}}}href"
_XLINK_LABEL = f"{{{_XLINK}}}label"
_XLINK_FROM = f"{{{_XLINK}}}from"
_XLINK_TO = f"{{{_XLINK}}}to"
_XLINK_ROLE = f"{{{_XLINK}}}role"


def _concept_from_href(href: str) -> str | None:
    if not href or "#" not in href:
        return None
    fragment = href.rsplit("#", 1)[-1]
    if "_" not in fragment:
        return fragment
    parts = fragment.split("_", 2)
    if len(parts) == 3:
        prefix = f"{parts[0]}_{parts[1]}"
        local = parts[2]
    else:
        prefix, local = fragment.split("_", 1)
    return f"{prefix}/{local}"


def _statement_from_role(role: str) -> str | None:
    lower = role.lower()
    if "balancesheet" in lower or "financialposition" in lower:
        return "BalanceSheet"
    if "cashflow" in lower:
        return "CashFlow"
    if "incomestatement" in lower or "profit" in lower or "loss" in lower:
        return "IncomeStatement"
    if "equity" in lower:
        return "Equity"
    return None


def _parse_cal_xml(path: Path | None) -> dict[str, tuple]:
    if path is None or not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    parent_map: dict[str, tuple[str, float, str | None]] = {}
    for link in root.iter(f"{{{_LINK_NS}}}calculationLink"):
        role = link.get(_XLINK_ROLE, "")
        locs: dict[str, str] = {}
        for loc in link.iter(f"{{{_LINK_NS}}}loc"):
            label = loc.get(_XLINK_LABEL)
            concept = _concept_from_href(loc.get(_XLINK_HREF, ""))
            if label and concept:
                locs[label] = concept
        for arc in link.iter(f"{{{_LINK_NS}}}calculationArc"):
            parent = locs.get(arc.get(_XLINK_FROM, ""))
            child = locs.get(arc.get(_XLINK_TO, ""))
            if not parent or not child:
                continue
            try:
                weight = float(arc.get("weight", "1"))
            except ValueError:
                weight = 1.0
            parent_map[child] = (parent, weight, role)

    def chain_for(concept: str) -> list[str]:
        chain = [concept]
        seen = {concept}
        current = concept
        for _ in range(30):
            parent = parent_map.get(current, (None, None, None))[0]
            if parent is None or parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
            current = parent
        return list(reversed(chain))

    result: dict[str, tuple] = {}
    all_concepts = set(parent_map) | {parent for parent, _, _ in parent_map.values()}
    for concept in all_concepts:
        chain = chain_for(concept)
        parent, weight, role = parent_map.get(concept, (None, 1.0, None))
        local_path = ", ".join(item.split("/", 1)[-1] for item in chain)
        result[concept] = (
            parent,
            chain[0],
            f"[{local_path}]",
            weight,
            _statement_from_role(role or ""),
            len(chain) - 1,
            1.0,
        )
    return result


def _parse_pre_xml(path: Path | None) -> dict[str, tuple]:
    if path is None or not path.exists():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    result: dict[str, tuple] = {}
    parent_by_child: dict[str, str | None] = {}
    order_by_child: dict[str, int] = {}
    role_by_child: dict[str, str | None] = {}
    position_by_child: dict[str, int] = {}
    position = 0
    for link in root.iter(f"{{{_LINK_NS}}}presentationLink"):
        role = link.get(_XLINK_ROLE, "")
        locs: dict[str, str] = {}
        for loc in link.iter(f"{{{_LINK_NS}}}loc"):
            label = loc.get(_XLINK_LABEL)
            concept = _concept_from_href(loc.get(_XLINK_HREF, ""))
            if label and concept:
                locs[label] = concept
        for arc in link.iter(f"{{{_LINK_NS}}}presentationArc"):
            parent = locs.get(arc.get(_XLINK_FROM, ""))
            child = locs.get(arc.get(_XLINK_TO, ""))
            if not child:
                continue
            position += 1
            try:
                order = round(float(arc.get("order", str(position))))
            except ValueError:
                order = position
            parent_by_child[child] = parent
            order_by_child[child] = order
            role_by_child[child] = _statement_from_role(role)
            position_by_child[child] = position
    for child, parent in parent_by_child.items():
        level = 0
        current = parent
        seen = {child}
        while current and current in parent_by_child and current not in seen:
            seen.add(current)
            level += 1
            current = parent_by_child.get(current)
        result[child] = (
            parent,
            order_by_child.get(child),
            level,
            role_by_child.get(child),
            position_by_child.get(child),
        )
    return result


def _xbrl_buffer(path: Path) -> BytesIO:
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp932", "euc-jp", "latin-1"):
        try:
            text = raw.decode(encoding)
            if encoding != "utf-8":
                text = re.sub(r'encoding=["\'][^"\']+["\']', 'encoding="UTF-8"', text, count=1)
                raw = text.encode("utf-8")
            return BytesIO(raw)
        except UnicodeDecodeError:
            continue
    return BytesIO(raw)


def _derive_fiscal_period(period_start: date | None, period_end: date, filing_type: str) -> tuple[int, str]:
    if period_start is None:
        if filing_type in {"160", "170"}:
            return period_end.year, "H1"
        if filing_type in {"140", "150"}:
            return period_end.year, f"Q{((period_end.month - 1) // 3) + 1}"
        return period_end.year, "FY"
    days = (period_end - period_start).days + 1
    if days >= 330:
        return period_end.year, "FY"
    if 150 <= days <= 220:
        return period_end.year, "H1"
    if days <= 120:
        return period_end.year, f"Q{((period_end.month - 1) // 3) + 1}"
    return period_end.year, "OTHER"


def _qname_text(value: str | None) -> str:
    return (value or "").strip()


def _is_nil(elem: ET.Element) -> bool:
    value = elem.get("{http://www.w3.org/2001/XMLSchema-instance}nil")
    return value is not None and value.lower() in {"true", "1"}


def _parse_root_and_namespaces(path: Path) -> tuple[ET.Element, dict[str, str]]:
    buf = _xbrl_buffer(path)
    ns_map: dict[str, str] = {}
    try:
        for _, (prefix, uri) in ET.iterparse(buf, events=["start-ns"]):
            if prefix and uri and uri not in ns_map:
                ns_map[uri] = prefix
    except Exception as exc:
        raise ValueError(f"Could not parse namespace declarations in {path}: {exc}") from exc
    buf.seek(0)
    return ET.parse(buf).getroot(), ns_map


def extract_identity_metadata(item: EdinetXbrlFile) -> dict[str, str]:
    root, ns_map = _parse_root_and_namespaces(item.path)
    values: dict[str, str] = {}
    for elem in root:
        if not elem.tag.startswith("{") or _is_nil(elem):
            continue
        ns_uri, local = elem.tag[1:].split("}", 1)
        if ns_uri == _XBRLI:
            continue
        prefix = ns_map.get(ns_uri) or ns_uri.rstrip("/").rsplit("/", 1)[-1]
        concept_id = f"{prefix}/{local}"
        field = _IDENTITY_CONCEPTS.get(concept_id)
        if not field:
            continue
        raw_value = (elem.text or "").strip()
        if raw_value:
            values.setdefault(field, raw_value)
    return {
        "name_en": values.get("name_en") or values.get("name_en_cover"),
        "name": values.get("name") or values.get("name_cover"),
        "sec_code": values.get("sec_code"),
    }


def _dimension_signature(ctx) -> str:
    dims: list[dict[str, str]] = []
    for container_name in ("scenario", "segment"):
        container = ctx.find(f"{{{_XBRLI}}}{container_name}")
        if container is None:
            continue
        for member in container.findall(f".//{{{_XBRLDI}}}explicitMember"):
            dims.append(
                {
                    "container": container_name,
                    "type": "explicit",
                    "dimension": _qname_text(member.get("dimension") or member.get(f"{{{_XBRLDI}}}dimension")),
                    "member": _qname_text(member.text),
                }
            )
        for member in container.findall(f".//{{{_XBRLDI}}}typedMember"):
            child = next(iter(member), None)
            dims.append(
                {
                    "container": container_name,
                    "type": "typed",
                    "dimension": _qname_text(member.get("dimension") or member.get(f"{{{_XBRLDI}}}dimension")),
                    "member": ET.tostring(child, encoding="unicode") if child is not None else "",
                }
            )
    if not dims:
        return ""
    dims.sort(key=lambda item: (item["container"], item["type"], item["dimension"], item["member"]))
    return json.dumps(dims, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_xbrl_file(
    item: EdinetXbrlFile,
    stats: dict[str, int] | None = None,
) -> list[dict]:
    cal_map = _parse_cal_xml(item.cal_path)
    pre_map = _parse_pre_xml(item.pre_path)
    root, ns_map = _parse_root_and_namespaces(item.path)

    contexts: dict[str, tuple[date, date | None, int, str]] = {}
    for ctx in root.iter(f"{{{_XBRLI}}}context"):
        ctx_id = ctx.get("id")
        period = ctx.find(f"{{{_XBRLI}}}period")
        if not ctx_id or period is None:
            continue
        instant_el = period.find(f"{{{_XBRLI}}}instant")
        start_el = period.find(f"{{{_XBRLI}}}startDate")
        end_el = period.find(f"{{{_XBRLI}}}endDate")
        try:
            if instant_el is not None and instant_el.text:
                period_end = date.fromisoformat(instant_el.text.strip())
                period_start = None
            elif end_el is not None and end_el.text:
                period_end = date.fromisoformat(end_el.text.strip())
                period_start = date.fromisoformat(start_el.text.strip()) if start_el is not None and start_el.text else None
            else:
                continue
        except ValueError:
            continue

        tier = 0
        for dim_container in (ctx.find(f"{{{_XBRLI}}}scenario"), ctx.find(f".//{{{_XBRLI}}}segment")):
            if dim_container is None:
                continue
            members = dim_container.findall(f".//{{{_XBRLDI}}}explicitMember")
            if not members:
                continue
            member_text = " ".join((member.text or "") for member in members)
            tier = 1 if "NonConsolidated" in member_text else 2
            break
        contexts[ctx_id] = (period_end, period_start, tier, _dimension_signature(ctx))

    units: dict[str, str] = {}
    for unit_el in root.iter(f"{{{_XBRLI}}}unit"):
        unit_id = unit_el.get("id")
        measure = unit_el.findtext(f"{{{_XBRLI}}}measure")
        if unit_id and measure:
            units[unit_id] = measure.split(":", 1)[-1]

    rows: list[dict] = []
    ifrs_count = 0
    for elem in root:
        if not elem.tag.startswith("{"):
            continue
        ns_uri, local = elem.tag[1:].split("}", 1)
        if ns_uri == _XBRLI or any(local.endswith(suffix) for suffix in _NON_NUMERIC_SUFFIXES):
            continue
        ctx_ref = elem.get("contextRef")
        unit_ref = elem.get("unitRef")
        if not ctx_ref or ctx_ref not in contexts or not unit_ref or _is_nil(elem):
            continue
        raw_value = (elem.text or "").strip()
        if not raw_value:
            continue
        try:
            numeric = Decimal(raw_value.replace(",", ""))
        except InvalidOperation:
            continue
        try:
            decimals = int(elem.get("decimals", "0"))
        except (TypeError, ValueError):
            decimals = None

        prefix = ns_map.get(ns_uri) or ns_uri.rstrip("/").rsplit("/", 1)[-1]
        if "ifrs" in prefix.lower():
            ifrs_count += 1
        concept_id = f"{prefix}/{local}"
        period_end, period_start, context_tier, dimension_signature = contexts[ctx_ref]
        if stats is not None:
            stats["kept"] = stats.get("kept", 0) + 1
        fiscal_year, fiscal_period = _derive_fiscal_period(period_start, period_end, item.filing_type)
        if fiscal_period in {"FY", "INSTANT"} and item.filing_type in _INTERIM_DOC_TYPES:
            fiscal_period = "H1" if item.filing_type in {"160", "170"} else f"Q{((period_end.month - 1) // 3) + 1}"
        cal = cal_map.get(concept_id)
        pre = pre_map.get(concept_id)
        parent_id = cal[0] if cal else None
        root_id = cal[1] if cal else concept_id
        concept_path = cal[2] if cal else f"[{local}]"
        weight = cal[3] if cal else 1
        statement_type = cal[4] if cal else (pre[3] if pre else None)
        concept_id_level = cal[5] if cal else 0
        effective_weight = cal[6] if cal else 1
        rows.append(
            {
                "edinet_code": item.edinet_code,
                "concept_id": concept_id,
                "period_end": period_end,
                "fiscal_period": fiscal_period,
                "value_type": "REST" if item.filing_type in _AMENDMENT_DOC_TYPES else "ORIG",
                "filing_id": item.doc_id,
                "filing_type": item.filing_type,
                "period_start": period_start,
                "fiscal_year": fiscal_year,
                "source_fp": None,
                "value": numeric,
                "unit": units.get(unit_ref, unit_ref),
                "decimals": decimals,
                "filed_date": item.filed_date,
                "taxonomy": None,
                "context_tier": context_tier,
                "context_id": ctx_ref,
                "dimension_signature": dimension_signature,
                "statement_type": statement_type,
                "parent_id": parent_id,
                "root_id": root_id,
                "concept_path": concept_path,
                "concept_id_level": concept_id_level,
                "weight": weight,
                "effective_weight": effective_weight,
                "pre_parent_id": pre[0] if pre else None,
                "pre_order": pre[1] if pre else None,
                "pre_level": pre[2] if pre else None,
                "pre_position": pre[4] if pre else None,
            }
        )
    taxonomy = "ifrs" if rows and ifrs_count > len(rows) / 2 else "jgaap"
    for row in rows:
        row["taxonomy"] = taxonomy
    return rows


def parse_and_write_jp(item: EdinetXbrlFile) -> int:
    return upsert_jp_facts(parse_xbrl_file(item))
