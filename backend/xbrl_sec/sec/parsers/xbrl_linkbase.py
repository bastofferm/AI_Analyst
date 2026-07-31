"""Shared XBRL calculation, presentation, and definition linkbase parsing.

Two flavors of output:

- ``parse_cal_xml`` / ``parse_pre_xml`` / ``parse_def_xml`` return a compact
  per-concept summary used by the existing raw fact writers (denormalized).
- ``iter_cal_arcs`` / ``iter_pre_arcs`` / ``iter_def_arcs`` return one raw
  arc tuple per linkbase edge, suitable for normalized persistence into
  ``sec.ref_xbrl_relationship_edge`` (Phase 2 of the M:1 refactor).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import xml.etree.ElementTree as ET


_LINK_NS = "http://www.xbrl.org/2003/linkbase"
_XLINK = "http://www.w3.org/1999/xlink"
_XBRLDT = "http://xbrl.org/2005/xbrldt"
_XLINK_HREF = f"{{{_XLINK}}}href"
_XLINK_LABEL = f"{{{_XLINK}}}label"
_XLINK_FROM = f"{{{_XLINK}}}from"
_XLINK_TO = f"{{{_XLINK}}}to"
_XLINK_ROLE = f"{{{_XLINK}}}role"
_XLINK_ARCROLE = f"{{{_XLINK}}}arcrole"
_XBRLDT_USABLE = f"{{{_XBRLDT}}}usable"

_DIMENSION_ARCROLES = frozenset({
    "http://xbrl.org/int/dim/arcrole/all",
    "http://xbrl.org/int/dim/arcrole/notAll",
    "http://xbrl.org/int/dim/arcrole/hypercube-dimension",
    "http://xbrl.org/int/dim/arcrole/dimension-domain",
    "http://xbrl.org/int/dim/arcrole/dimension-default",
    "http://xbrl.org/int/dim/arcrole/domain-member",
})


def concept_from_href(href: str) -> str | None:
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


def statement_from_role(role: str) -> str | None:
    lower = role.lower()
    if "balancesheet" in lower or "financialposition" in lower:
        return "BalanceSheet"
    if "cashflow" in lower:
        return "CashFlow"
    if "incomestatement" in lower or "income" in lower or "profit" in lower or "loss" in lower:
        return "IncomeStatement"
    if "equity" in lower:
        return "Equity"
    return None


def parse_cal_xml(path: Path | None) -> dict[str, tuple]:
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
            concept = concept_from_href(loc.get(_XLINK_HREF, ""))
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
            statement_from_role(role or ""),
            len(chain) - 1,
            1.0,
        )
    return result


def parse_pre_xml(path: Path | None) -> dict[str, tuple]:
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
            concept = concept_from_href(loc.get(_XLINK_HREF, ""))
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
            role_by_child[child] = statement_from_role(role)
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


def _open_root(path: Path | None) -> ET.Element | None:
    if path is None or not path.exists():
        return None
    try:
        return ET.parse(path).getroot()
    except Exception:
        return None


def parse_def_xml(path: Path | None) -> dict[str, list[dict[str, str | None]]]:
    """Parse a definition linkbase into per-concept dimensional context.

    Returns ``{concept_id: [{"axis": ..., "member": ..., "role": ...}, ...]}``
    where each entry records that the concept appears under that axis/member
    in some definition arc. Useful as a quick lookup for whether two facts
    share dimension scope. For full arc-level access use ``iter_def_arcs``.
    """
    root = _open_root(path)
    if root is None:
        return {}
    out: dict[str, list[dict[str, str | None]]] = {}
    for arc in iter_def_arcs(path):
        member = arc.get("dimension_member") or arc.get("child_concept_id")
        if not member:
            continue
        out.setdefault(member, []).append({
            "axis": arc.get("dimension_axis"),
            "member": arc.get("dimension_member"),
            "role": arc.get("role_uri"),
            "arcrole": arc.get("arcrole"),
        })
    return out


def _iter_linkbase_arcs(
    path: Path | None,
    link_tag: str,
    arc_tag: str,
) -> Iterator[tuple[ET.Element, dict[str, str], ET.Element]]:
    """Yield (link, locs, arc) tuples for each arc in the linkbase."""
    root = _open_root(path)
    if root is None:
        return
    for link in root.iter(f"{{{_LINK_NS}}}{link_tag}"):
        locs: dict[str, str] = {}
        for loc in link.iter(f"{{{_LINK_NS}}}loc"):
            label = loc.get(_XLINK_LABEL)
            concept = concept_from_href(loc.get(_XLINK_HREF, ""))
            if label and concept:
                locs[label] = concept
        for arc in link.iter(f"{{{_LINK_NS}}}{arc_tag}"):
            yield link, locs, arc


def iter_cal_arcs(path: Path | None) -> Iterator[dict[str, str | float | None]]:
    """Yield one dict per calculation arc, suitable for normalized persistence."""
    for link, locs, arc in _iter_linkbase_arcs(path, "calculationLink", "calculationArc"):
        parent = locs.get(arc.get(_XLINK_FROM, ""))
        child = locs.get(arc.get(_XLINK_TO, ""))
        if not parent or not child:
            continue
        try:
            weight: float | None = float(arc.get("weight", "1"))
        except ValueError:
            weight = None
        try:
            order = float(arc.get("order")) if arc.get("order") is not None else None
        except ValueError:
            order = None
        yield {
            "linkbase_type": "calculation",
            "role_uri": link.get(_XLINK_ROLE),
            "arcrole": arc.get(_XLINK_ARCROLE),
            "parent_concept_id": parent,
            "child_concept_id": child,
            "weight": weight,
            "order_index": order,
            "preferred_label": None,
            "dimension_axis": None,
            "dimension_member": None,
            "usable": None,
        }


def iter_pre_arcs(path: Path | None) -> Iterator[dict[str, str | float | None]]:
    """Yield one dict per presentation arc, suitable for normalized persistence."""
    for link, locs, arc in _iter_linkbase_arcs(path, "presentationLink", "presentationArc"):
        parent = locs.get(arc.get(_XLINK_FROM, ""))
        child = locs.get(arc.get(_XLINK_TO, ""))
        if not child:
            continue
        try:
            order = float(arc.get("order")) if arc.get("order") is not None else None
        except ValueError:
            order = None
        yield {
            "linkbase_type": "presentation",
            "role_uri": link.get(_XLINK_ROLE),
            "arcrole": arc.get(_XLINK_ARCROLE),
            "parent_concept_id": parent,
            "child_concept_id": child,
            "weight": None,
            "order_index": order,
            "preferred_label": arc.get("preferredLabel"),
            "dimension_axis": None,
            "dimension_member": None,
            "usable": None,
        }


def iter_def_arcs(path: Path | None) -> Iterator[dict[str, str | float | bool | None]]:
    """Yield one dict per definition arc.

    XBRL Dimensions arcroles are recognized and the parent/child are mapped
    into dimension_axis / dimension_member where applicable:

    - ``hypercube-dimension`` arcs: parent is hypercube, child is axis. The
      child concept is emitted as dimension_axis.
    - ``dimension-domain`` arcs: parent is axis, child is domain root.
    - ``domain-member`` arcs: parent is axis or domain, child is member.
    - ``dimension-default``, ``all``, ``notAll`` arcs are recorded with their
      arcrole for downstream interpretation.
    """
    for link, locs, arc in _iter_linkbase_arcs(path, "definitionLink", "definitionArc"):
        parent = locs.get(arc.get(_XLINK_FROM, ""))
        child = locs.get(arc.get(_XLINK_TO, ""))
        if not child:
            continue
        arcrole = arc.get(_XLINK_ARCROLE)
        try:
            order = float(arc.get("order")) if arc.get("order") is not None else None
        except ValueError:
            order = None
        usable_raw = arc.get(_XBRLDT_USABLE)
        usable: bool | None
        if usable_raw is None:
            usable = None
        else:
            usable = str(usable_raw).strip().lower() in {"true", "1"}

        axis: str | None = None
        member: str | None = None
        if arcrole == "http://xbrl.org/int/dim/arcrole/hypercube-dimension":
            axis = child
        elif arcrole == "http://xbrl.org/int/dim/arcrole/dimension-domain":
            axis = parent
            member = child
        elif arcrole == "http://xbrl.org/int/dim/arcrole/domain-member":
            axis = parent
            member = child
        elif arcrole == "http://xbrl.org/int/dim/arcrole/dimension-default":
            axis = parent
            member = child

        yield {
            "linkbase_type": "definition",
            "role_uri": link.get(_XLINK_ROLE),
            "arcrole": arcrole,
            "parent_concept_id": parent,
            "child_concept_id": child,
            "weight": None,
            "order_index": order,
            "preferred_label": None,
            "dimension_axis": axis,
            "dimension_member": member,
            "usable": usable,
        }
