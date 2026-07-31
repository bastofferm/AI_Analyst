"""Seed standardized line items and metrics from local registry files.

The database reference tables are the runtime source of truth.  This module is
additive/upsert-only for reference data and must not truncate
ref_standardized_line_items.
"""
from __future__ import annotations

import json
import pprint
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics import formulas as existing_formulas
from xbrl_sec.sec.settings import load_settings

_HIERARCHY_SPECS = {
    "income_statement":    "is_hierarchy_spec.json",
    "balance_sheet":       "bs_hierarchy_spec.json",
    "cash_flow_statement": "cf_hierarchy_spec.json",
}

_ITEM_CLASS_DERIVATION = {
    "leaf":               "prefer_filed",
    "intermediate":       "prefer_filed",
    "catch_all":          "residual",
    "supplemental":       "prefer_filed",
    "cross_statement_ref":"prefer_filed",
}


_REGISTRY_NAME = "line_item_metric_registry.json"
_TOKEN_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b")
_CATEGORY_BY_STATEMENT = {
    "balance_sheet": "balance_sheet",
    "income_statement": "income_statement",
    "cash_flow_statement": "cash_flow",
    "cash_flow": "cash_flow",
    "derived": "derived",
    "market": "market",
    "operating_kpi": "operating_kpi",
}


def _registry_path(path: str | None = None) -> Path:
    if path:
        return Path(path)
    return load_settings().project_root / "spec" / _REGISTRY_NAME


def _load_registry(path: str | None = None) -> dict[str, Any]:
    return json.loads(_registry_path(path).read_text(encoding="utf-8"))


def _flatten(section: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

    walk(section)
    return out


def _std_path(item: dict[str, Any]) -> str:
    statement = item.get("statement_type") or "unknown"
    scope = item.get("sector_scope") or "universal"
    return f"[{statement},{scope},{item['id']}]"


def _line_item_rows(registry: dict[str, Any]) -> list[tuple]:
    version = registry.get("version")
    source = registry.get("spec_id")
    rows_by_id = {}
    items = _flatten(registry["line_item_registry"]) + _flatten(registry.get("is_gap_registry", {}))
    for item in items:
        statement = item.get("statement_type")
        row = (
            item["id"],
            _CATEGORY_BY_STATEMENT.get(statement, statement),
            item.get("label"),
            item.get("description"),
            bool(item.get("is_filed", True)),
            item.get("importance") or {"P1": 1, "P2": 2, "P3": 3}.get(item.get("priority")),
            None,
            item.get("sector_scope") or "universal",
            item.get("unit_type"),
            _std_path(item),
            statement,
            item.get("sector_scope") or "universal",
            item.get("gics_sector"),
            item.get("maps_into_metrics") or [],
            version,
            source,
            item.get("display_order"),
        )
        if item["id"] not in rows_by_id or item.get("is_gap_addition"):
            rows_by_id[item["id"]] = row

    # Registry v1 contains this rename target but omits the filed line item row.
    # Keep the registry internally referential until the JSON is corrected.
    rename_targets = set((registry.get("rename_map") or {}).get("line_items", {}).values())
    existing = set(rows_by_id)
    if "nonperforming_loan_ratio_filed" in rename_targets and "nonperforming_loan_ratio_filed" not in existing:
        rows_by_id["nonperforming_loan_ratio_filed"] = (
            "nonperforming_loan_ratio_filed",
            "operating_kpi",
            "Nonperforming Loan Ratio (Filed)",
            "Filed nonperforming loan ratio when disclosed directly by a bank.",
            True,
            2,
            None,
            "gics_40_banks",
            "RATIO",
            "[operating_kpi,gics_40_banks,nonperforming_loan_ratio_filed]",
            "operating_kpi",
            "gics_40_banks",
            "40_banks",
            ["nonperforming_loan_ratio"],
            version,
            source,
            None,
        )
    return list(rows_by_id.values())


def _metric_rows(registry: dict[str, Any]) -> list[tuple]:
    version = registry.get("version")
    source = registry.get("spec_id")
    rows = []
    for item in _flatten(registry["metric_registry"]):
        rows.append(
            (
                item["id"],
                item.get("category"),
                item.get("label"),
                item.get("importance"),
                item.get("formula_symbolic") or item.get("formula_sql"),
                item.get("required_line_items") or [],
                item.get("description"),
                item.get("unit_type"),
                "FNDM",
                item.get("formula_symbolic"),
                item.get("formula_sql"),
                item.get("sector_scope") or "universal",
                item.get("gics_sector"),
                item.get("interpretation"),
                item.get("academic_source"),
                version,
                source,
            )
        )
    return rows


def _rename_pairs(mapping: dict[str, str]) -> list[tuple[str, str]]:
    return [(old, new) for old, new in mapping.items() if old and new and old != new]


def _apply_line_item_renames(cur, pairs: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, column in (
        ("map_concept_to_taxonomy", "target_variable"),
        ("map_concept_to_taxonomy_review_queue", "suggested_target_variable"),
    ):
        total = 0
        for old, new in pairs:
            cur.execute(f"UPDATE {table} SET {column}=%s WHERE {column}=%s", (new, old))
            total += cur.rowcount
        counts[f"{table}.{column}"] = total
    return counts


def _edge_reference_gap_counts(cur) -> dict[str, int]:
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE p.line_item_id IS NULL) AS missing_parents,
            COUNT(*) FILTER (WHERE c.line_item_id IS NULL) AS missing_children,
            COUNT(*) FILTER (WHERE e.parent_id = e.child_id) AS self_edges
        FROM ref_std_item_edge e
        LEFT JOIN ref_standardized_line_items p ON p.line_item_id = e.parent_id
        LEFT JOIN ref_standardized_line_items c ON c.line_item_id = e.child_id
        """
    )
    row = cur.fetchone()
    return {
        "missing_parents": int(row[0] or 0),
        "missing_children": int(row[1] or 0),
        "self_edges": int(row[2] or 0),
    }


def _edge_cycle_count(cur) -> int:
    cur.execute(
        """
        SELECT parent_id, child_id, statement_type, sector_scope, accounting_standard
        FROM ref_std_item_edge
        WHERE edge_type = 'rollup'
        """
    )
    children: dict[tuple[str, str, str | None, str], list[str]] = {}
    for parent_id, child_id, statement_type, sector_scope, accounting_standard in cur.fetchall():
        key = (statement_type, sector_scope, accounting_standard, parent_id)
        children.setdefault(key, []).append(child_id)

    cycle_count = 0
    for statement_type, sector_scope, accounting_standard, parent_id in list(children):
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            edge_key = (statement_type, sector_scope, accounting_standard, node)
            has_cycle = any(visit(child) for child in children.get(edge_key, []))
            visiting.remove(node)
            visited.add(node)
            return has_cycle

        if visit(parent_id):
            cycle_count += 1
    return cycle_count


_STATEMENT_BASE_ORDER = {
    "income_statement": 10_000,
    "balance_sheet": 20_000,
    "cash_flow_statement": 30_000,
}

_SECTION_BASE_ORDER = {
    "current_assets": 1,
    "noncurrent_assets": 2,
    "assets": 3,           # total_assets (is_current=None)
    "current_liabilities": 4,
    "noncurrent_liabilities": 5,
    "liabilities": 6,      # total_liabilities
    "equity": 7,
    "operating": 1,
    "investing": 2,
    "financing": 3,
    "cash_reconciliation": 4,
    "supplemental": 5,
}


def _display_order_rows_from_specs(spec_root: Path) -> dict[str, dict[str, int]]:
    """Derive stable statement-level display order by canonical line item id."""
    result: dict[str, dict[str, int]] = {}

    for statement_type, spec_name in _HIERARCHY_SPECS.items():
        path = spec_root / spec_name
        if not path.exists():
            continue
        spec = json.loads(path.read_text(encoding="utf-8"))
        statement_base = _STATEMENT_BASE_ORDER.get(statement_type, 90_000)
        for item, _standard, _sector in _iter_spec_items(spec, statement_type):
            item_id = item.get("mzqa_canonical_id") or item.get("id")
            if not item_id:
                continue
            layer = item.get("is_layer") or {}
            within = item.get("within_layer") or item.get("within_section") or {}
            bs_section = item.get("bs_section")
            cf_section = item.get("cf_section")

            if isinstance(bs_section, dict):
                # BS spec: bs_section = {section, is_current, section_rank}
                sec = str(bs_section.get("section") or "").lower()
                is_curr = bs_section.get("is_current")
                if sec == "assets":
                    if is_curr is True:
                        section_norm = "current_assets"
                    elif is_curr is False:
                        section_norm = "noncurrent_assets"
                    else:
                        section_norm = "assets"
                elif sec == "liabilities":
                    if is_curr is True:
                        section_norm = "current_liabilities"
                    elif is_curr is False:
                        section_norm = "noncurrent_liabilities"
                    else:
                        section_norm = "liabilities"
                else:
                    section_norm = sec or "equity"
                major = _SECTION_BASE_ORDER.get(section_norm, 50)
                sibling = int(bs_section.get("section_rank") or 0)
            elif layer.get("layer_index") is not None:
                # IS spec: is_layer with layer_index
                section_norm = str(layer.get("layer_name") or "").lower().replace(" ", "_").replace("/", "_")
                major = int(layer.get("layer_index") or 0)
                try:
                    sibling = int(within.get("sibling_rank") or 0)
                except Exception:
                    sibling = 0
            else:
                # CF spec: cf_section is a string
                section_norm = str(cf_section or "").lower().replace(" ", "_").replace("/", "_")
                major = _SECTION_BASE_ORDER.get(section_norm, 50)
                try:
                    sibling = int(within.get("sibling_rank") or 0)
                except Exception:
                    sibling = 0
            candidate = statement_base + major * 100 + sibling
            by_standard = result.setdefault(item_id, {})
            if "display_order" not in by_standard or candidate < by_standard["display_order"]:
                by_standard["display_order"] = candidate
            standard_key = str(_standard or "").lower()
            if standard_key == "us_gaap":
                key = "display_order_us_gaap"
            elif standard_key == "jp_gaap":
                key = "display_order_jp_gaap"
            else:
                key = ""
            if key and (key not in by_standard or candidate < by_standard[key]):
                by_standard[key] = candidate
    return result


def _replace_token_ids(formula: str, replacements: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return replacements.get(token, token)

    return _TOKEN_RE.sub(repl, formula)


def _generate_formula_module(registry: dict[str, Any]) -> str:
    line_renames = dict(registry.get("rename_map", {}).get("line_items", {}))
    metric_renames = dict(registry.get("rename_map", {}).get("metrics", {}))
    metric_ids = {item["id"] for item in _flatten(registry["metric_registry"])}
    replacements = {**line_renames, **metric_renames}

    l1 = {
        line_renames.get(key, key): _replace_token_ids(value, replacements)
        for key, value in existing_formulas._L1_FORMULAS.items()
    }
    metric = {}
    for key, value in existing_formulas._METRIC_FORMULAS.items():
        new_key = metric_renames.get(key, key)
        if new_key not in metric_ids:
            continue
        metric[new_key] = _replace_token_ids(value, replacements)
    primary_required = {
        metric_renames.get(key, key): [line_renames.get(item, item) for item in values]
        for key, values in existing_formulas._METRIC_PRIMARY_REQUIRED.items()
        if metric_renames.get(key, key) in metric_ids
    }

    return (
        '"""Executable metric formulas generated from line_item_metric_registry.json.\n\n'
        "The registry controls public line item and metric IDs. The formulas are\n"
        "ported from the legacy executable formula set with registry rename_map\n"
        "applied mechanically.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        f"_LINE_ITEM_RENAMES = {pprint.pformat(line_renames, width=100)}\n\n"
        f"_METRIC_RENAMES = {pprint.pformat(metric_renames, width=100)}\n\n"
        f"_L1_FORMULAS = {pprint.pformat(l1, width=120)}\n\n"
        f"_METRIC_FORMULAS = {pprint.pformat(metric, width=120)}\n\n"
        f"_METRIC_PRIMARY_REQUIRED = {pprint.pformat(primary_required, width=120)}\n"
    )


def write_formula_module(registry: dict[str, Any]) -> Path:
    path = Path(__file__).resolve().parents[1] / "metrics" / "formulas.py"
    path.write_text(_generate_formula_module(registry), encoding="utf-8")
    return path


def sync_registry(path: str | None = None, update_formulas: bool = True) -> dict[str, int | str]:
    registry = _load_registry(path)
    line_rows = _line_item_rows(registry)
    metric_rows = _metric_rows(registry)
    line_renames = _rename_pairs(registry.get("rename_map", {}).get("line_items", {}))
    synced_at = datetime.now(timezone.utc)

    with connect() as conn, conn.cursor() as cur:
        rename_counts = _apply_line_item_renames(cur, line_renames)
        line_written = execute_values(
            cur,
            """
            INSERT INTO ref_standardized_line_items
                (line_item_id, category, label, description, is_filed, importance,
                 formula, mapping_sector, unit_type, std_concept_path, statement_type,
                 sector_scope, gics_sector, maps_into_metrics, registry_version,
                 registry_source, display_order, created_at, updated_at)
            VALUES %s
            ON CONFLICT (line_item_id) DO UPDATE SET
                category = EXCLUDED.category,
                label = EXCLUDED.label,
                description = EXCLUDED.description,
                is_filed = EXCLUDED.is_filed,
                importance = EXCLUDED.importance,
                formula = EXCLUDED.formula,
                mapping_sector = EXCLUDED.mapping_sector,
                unit_type = EXCLUDED.unit_type,
                std_concept_path = EXCLUDED.std_concept_path,
                statement_type = EXCLUDED.statement_type,
                sector_scope = EXCLUDED.sector_scope,
                gics_sector = EXCLUDED.gics_sector,
                maps_into_metrics = EXCLUDED.maps_into_metrics,
                registry_version = EXCLUDED.registry_version,
                registry_source = EXCLUDED.registry_source,
                display_order = COALESCE(EXCLUDED.display_order, ref_standardized_line_items.display_order),
                updated_at = now()
            """,
            [row + (synced_at, synced_at) for row in line_rows],
            page_size=1000,
        )
        metric_written = execute_values(
            cur,
            """
            INSERT INTO ref_metric_definitions
                (metric_id, category, name, importance, formula, required_line_items,
                 note, unit_type, metric_type, formula_symbolic, formula_sql,
                 sector_scope, gics_sector, interpretation, academic_source,
                 registry_version, registry_source, created_at, updated_at)
            VALUES %s
            ON CONFLICT (metric_id) DO UPDATE SET
                category = EXCLUDED.category,
                name = EXCLUDED.name,
                importance = EXCLUDED.importance,
                formula = EXCLUDED.formula,
                required_line_items = EXCLUDED.required_line_items,
                note = EXCLUDED.note,
                unit_type = EXCLUDED.unit_type,
                metric_type = EXCLUDED.metric_type,
                formula_symbolic = EXCLUDED.formula_symbolic,
                formula_sql = EXCLUDED.formula_sql,
                sector_scope = EXCLUDED.sector_scope,
                gics_sector = EXCLUDED.gics_sector,
                interpretation = EXCLUDED.interpretation,
                academic_source = EXCLUDED.academic_source,
                registry_version = EXCLUDED.registry_version,
                registry_source = EXCLUDED.registry_source,
                updated_at = now()
            """,
            [row + (synced_at, synced_at) for row in metric_rows],
            page_size=1000,
        )

    formula_path = write_formula_module(registry) if update_formulas else None
    edge_counts = sync_item_edges()
    out: dict[str, int | str] = {
        "line_items": line_written,
        "metrics": metric_written,
        "line_item_renames": sum(rename_counts.values()),
        **{f"edges_{k}": v for k, v in edge_counts.items()},
    }
    out.update(rename_counts)
    if formula_path:
        out["formula_module"] = str(formula_path)
    return out


def _spec_path(spec_name: str) -> Path:
    return load_settings().project_root / "spec" / spec_name


def _iter_spec_items(spec: dict[str, Any], statement_type: str):
    """Yield (item, standard, sector_scope) tuples from any hierarchy spec."""
    for sector_key, sector_data in spec.get("sectors", {}).items():
        for standard, std_data in sector_data.get("standards", {}).items():
            for item in std_data.get("items", []):
                yield item, standard, sector_key


def _edge_rows_from_spec(spec: dict[str, Any], statement_type: str, spec_source: str) -> list[tuple]:
    """Extract (parent_id, child_id, sign, statement_type, standard, sector, sibling_rank, spec_source) tuples."""
    rows: list[tuple] = []
    seen: set[tuple] = set()

    items_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item, standard, sector in _iter_spec_items(spec, statement_type):
        items_by_scope.setdefault((standard, sector), []).append(item)

    for (standard, sector), items in items_by_scope.items():
        canonical_by_id = {
            item["id"]: item.get("mzqa_canonical_id") or item["id"]
            for item in items
            if item.get("id")
        }
        for item in items:
            child_id = canonical_by_id.get(item["id"], item["id"])
            within = item.get("within_layer") or item.get("within_section") or {}
            raw_parent_id = within.get("parent_id")
            parent_id = canonical_by_id.get(raw_parent_id, raw_parent_id)
            sign = item.get("sign_on_parent") if item.get("sign_on_parent") is not None else item.get("sign_in_waterfall", 1)
            sibling_rank = within.get("sibling_rank", 1)

            if parent_id is None or parent_id == child_id or sign == 0:
                continue

            key = (parent_id, child_id, standard, sector)
            if key in seen:
                continue
            seen.add(key)

            rows.append((
                parent_id,
                child_id,
                int(sign),
                "rollup",
                statement_type,
                standard,
                sector,
                sibling_rank,
                spec_source,
            ))

    return rows


def _item_class_rows_from_spec(spec: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Return {item_id: (item_class, derivation_policy)} from any hierarchy spec."""
    result: dict[str, tuple[str, str]] = {}
    for item, _standard, _sector in _iter_spec_items(spec, ""):
        item_class = item.get("item_class")
        item_id = item.get("mzqa_canonical_id") or item["id"]
        if item_class and item_id not in result:
            policy = _ITEM_CLASS_DERIVATION.get(item_class, "prefer_filed")
            result[item_id] = (item_class, policy)
    return result


def _rollup_check_rows(all_edge_rows: list[tuple]) -> list[tuple]:
    """Derive ref_std_identity_check rows for every rollup parent group."""
    from collections import defaultdict as _dd
    groups: dict[tuple, list[tuple[str, int, int]]] = _dd(list)
    for (parent_id, child_id, sign, edge_type, stmt_type, acct_std, sector, sibling_rank, _src) in all_edge_rows:
        if edge_type == "rollup":
            groups[(parent_id, sector, acct_std, stmt_type)].append((child_id, sign, sibling_rank))

    rows = []
    for (parent_id, sector, acct_std, stmt_type), children in groups.items():
        children_ordered = sorted(children, key=lambda x: x[2])
        rows.append((
            f"rollup:{parent_id}:{sector}:{acct_std or ''}",
            f"Rollup consistency: {parent_id} = Σ children",
            stmt_type,
            parent_id,
            [c[0] for c in children_ordered],
            [c[1] for c in children_ordered],
            5,
            None,
            sector,
            acct_std,
        ))
    return rows


def sync_item_edges(spec_root: Path | None = None) -> dict[str, int]:
    """Load all three hierarchy specs and upsert edges + item_class into the DB."""
    root = spec_root or load_settings().project_root / "spec"
    all_edge_rows: list[tuple] = []
    item_class_map: dict[str, tuple[str, str]] = {}
    display_order_map = _display_order_rows_from_specs(root)

    for statement_type, spec_name in _HIERARCHY_SPECS.items():
        path = root / spec_name
        if not path.exists():
            continue
        spec = json.loads(path.read_text(encoding="utf-8"))
        all_edge_rows.extend(_edge_rows_from_spec(spec, statement_type, spec_name.replace(".json", "")))
        item_class_map.update(_item_class_rows_from_spec(spec))

    edges_written = 0
    item_class_updated = 0
    rollup_checks_written = 0
    if all_edge_rows:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM ref_std_item_edge
                WHERE spec_source IN ('is_hierarchy_spec', 'bs_hierarchy_spec', 'cf_hierarchy_spec')
                """
            )
            # Upsert edges
            edges_written = execute_values(
                cur,
                """
                INSERT INTO ref_std_item_edge
                    (parent_id, child_id, sign, edge_type, statement_type,
                     accounting_standard, sector_scope, sibling_rank, spec_source)
                VALUES %s
                ON CONFLICT (parent_id, child_id, COALESCE(accounting_standard, ''), sector_scope)
                DO UPDATE SET
                    sign           = EXCLUDED.sign,
                    sibling_rank   = EXCLUDED.sibling_rank,
                    spec_source    = EXCLUDED.spec_source
                """,
                all_edge_rows,
                page_size=2000,
            )

            # Update item_class and derivation_policy on ref_standardized_line_items
            class_rows = [
                (item_id, item_class, policy)
                for item_id, (item_class, policy) in item_class_map.items()
            ]
            if class_rows:
                execute_values(
                    cur,
                    """
                    UPDATE ref_standardized_line_items r
                       SET item_class        = v.item_class,
                           derivation_policy = v.derivation_policy,
                           updated_at        = now()
                      FROM (VALUES %s) AS v(line_item_id, item_class, derivation_policy)
                     WHERE r.line_item_id = v.line_item_id
                       AND (
                            r.item_class IS DISTINCT FROM v.item_class
                         OR r.derivation_policy IS DISTINCT FROM v.derivation_policy
                       )
                    """,
                    class_rows,
                    page_size=2000,
                )
                item_class_updated = len(class_rows)

            order_rows = [
                (
                    item_id,
                    values.get("display_order"),
                    values.get("display_order_us_gaap"),
                    values.get("display_order_jp_gaap"),
                )
                for item_id, values in display_order_map.items()
            ]
            if order_rows:
                execute_values(
                    cur,
                    """
                    UPDATE ref_standardized_line_items r
                       SET display_order = v.display_order,
                           display_order_us_gaap = v.display_order_us_gaap,
                           display_order_jp_gaap = v.display_order_jp_gaap,
                           updated_at = now()
                      FROM (VALUES %s) AS v(line_item_id, display_order, display_order_us_gaap, display_order_jp_gaap)
                     WHERE r.line_item_id = v.line_item_id
                       AND (
                            r.display_order IS DISTINCT FROM v.display_order
                         OR r.display_order_us_gaap IS DISTINCT FROM v.display_order_us_gaap
                         OR r.display_order_jp_gaap IS DISTINCT FROM v.display_order_jp_gaap
                       )
                    """,
                    order_rows,
                    page_size=2000,
                )

            gap_counts = _edge_reference_gap_counts(cur)
            gap_counts["cycles"] = _edge_cycle_count(cur)
            if any(gap_counts.values()):
                raise RuntimeError(
                    "Hierarchy edge reference validation failed: "
                    + ", ".join(f"{key}={value}" for key, value in gap_counts.items())
                )

        rollup_rows = _rollup_check_rows(all_edge_rows)
        if rollup_rows:
            with connect() as conn, conn.cursor() as cur:
                rollup_checks_written = execute_values(
                    cur,
                    """
                    INSERT INTO ref_std_identity_check
                        (check_id, description, statement_type, lhs_item_id,
                         rhs_item_ids, rhs_signs, tolerance_bp, cross_statement,
                         sector_scope, accounting_standard)
                    VALUES %s
                    ON CONFLICT (check_id) DO UPDATE SET
                        rhs_item_ids = EXCLUDED.rhs_item_ids,
                        rhs_signs    = EXCLUDED.rhs_signs
                    """,
                    rollup_rows,
                    page_size=500,
                )

    return {"edges": edges_written, "item_class_updated": item_class_updated, "rollup_checks": rollup_checks_written}
