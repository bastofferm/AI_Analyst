"""Graph-closure derivation pass for the standardized fact layer.

After the raw mapping pass (tier1/tier2) populates leaf values, this module
walks the ref_std_item_edge tree to:
  1. Compute missing intermediate/subtotal values bottom-up from their children.
  2. Derive missing catch_all leaves top-down (parent − known siblings).
  3. Check accounting identity constraints and emit violations.

All computations happen at the (entity, fiscal_year, fiscal_period) grain.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics.compute import _eval_formula, _with_legacy_aliases
from xbrl_sec.sec.metrics.formulas import _L1_FORMULAS


_PARTIAL_THRESHOLD = Decimal("0.80")
_ROLLUP_TOLERANCE_BP = Decimal("5")


class _Edge:
    __slots__ = (
        "parent_id",
        "child_id",
        "sign",
        "sector_scope",
        "accounting_standard",
        "sibling_rank",
        "child_item_class",
        "child_derivation_policy",
    )

    def __init__(self, parent_id: str, child_id: str, sign: int,
                 sector_scope: str, accounting_standard: str | None, sibling_rank: int,
                 child_item_class: str | None = None, child_derivation_policy: str | None = None):
        self.parent_id = parent_id
        self.child_id = child_id
        self.sign = sign
        self.sector_scope = sector_scope
        self.accounting_standard = accounting_standard
        self.sibling_rank = sibling_rank
        self.child_item_class = child_item_class
        self.child_derivation_policy = child_derivation_policy


def load_edges() -> list[_Edge]:
    """Load all rollup edges from the DB once at pipeline startup."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.parent_id, e.child_id, e.sign, e.sector_scope, e.accounting_standard,
                   e.sibling_rank, li.item_class, li.derivation_policy
            FROM ref_std_item_edge e
            LEFT JOIN ref_standardized_line_items li ON li.line_item_id = e.child_id
            WHERE edge_type = 'rollup'
            ORDER BY e.parent_id, e.sibling_rank
            """
        )
        return [_Edge(*row) for row in cur.fetchall()]


def load_identity_checks() -> list[dict[str, Any]]:
    """Load identity check definitions from the DB."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT check_id, lhs_item_id, rhs_item_ids, rhs_signs, tolerance_bp,
                   sector_scope, accounting_standard, cross_statement
            FROM ref_std_identity_check
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _edge_contribution(value: Decimal, sign: int) -> Decimal:
    """Apply model edge sign without double-counting canonical expense signs.

    Standardized expense facts are often already stored negative. A subtractive
    model edge should therefore use the magnitude of the child and apply the
    edge sign once. Additive gain/loss rows keep their natural sign.
    """
    return -abs(value) if sign < 0 else value


def _is_residual_child(edge: _Edge) -> bool:
    return (
        edge.child_item_class == "catch_all"
        or edge.child_derivation_policy == "residual"
        or edge.child_id.startswith("other_")
    )


def validate_hierarchy_graph() -> None:
    """Fail fast when hierarchy edges cannot resolve to standardized line items."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE e.parent_id = e.child_id) AS self_edges,
                COUNT(*) FILTER (WHERE p.line_item_id IS NULL) AS missing_parents,
                COUNT(*) FILTER (WHERE c.line_item_id IS NULL) AS missing_children
            FROM ref_std_item_edge e
            LEFT JOIN ref_standardized_line_items p ON p.line_item_id = e.parent_id
            LEFT JOIN ref_standardized_line_items c ON c.line_item_id = e.child_id
            """
        )
        self_edges, missing_parents, missing_children = cur.fetchone()
    gaps = {
        "self_edges": int(self_edges or 0),
        "missing_parents": int(missing_parents or 0),
        "missing_children": int(missing_children or 0),
    }
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT parent_id, child_id, statement_type, sector_scope, accounting_standard
            FROM ref_std_item_edge
            WHERE edge_type = 'rollup'
            """
        )
        children: dict[tuple[str, str, str | None, str], list[str]] = {}
        for parent_id, child_id, statement_type, sector_scope, accounting_standard in cur.fetchall():
            children.setdefault((statement_type, sector_scope, accounting_standard, parent_id), []).append(child_id)

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

    gaps["cycles"] = cycle_count
    if any(gaps.values()):
        raise RuntimeError(
            "Hierarchy edge reference validation failed: "
            + ", ".join(f"{key}={value}" for key, value in gaps.items())
        )


def _relevant_edges(
    edges: list[_Edge],
    sector_scope: str,
    accounting_standard: str | None,
) -> tuple[dict[str, list[_Edge]], dict[str, str]]:
    """Return (children_by_parent, parent_by_child) filtered to this sector × standard."""
    children: dict[str, list[_Edge]] = {}
    parent_of: dict[str, str] = {}
    for e in edges:
        if e.sector_scope not in (sector_scope, "universal"):
            continue
        if e.accounting_standard is not None and e.accounting_standard != accounting_standard:
            continue
        children.setdefault(e.parent_id, []).append(e)
        parent_of[e.child_id] = e.parent_id
    return children, parent_of


def close_graph(
    filed: dict[str, Decimal],
    edges: list[_Edge],
    identity_checks: list[dict[str, Any]],
    sector_scope: str,
    accounting_standard: str | None,
) -> tuple[dict[str, tuple[Decimal, str]], list[dict[str, Any]]]:
    """Derive missing values and check identities for one entity×period.

    Returns:
        derived: {line_item_id: (value, metric_type)} for newly computed items
        violations: list of identity check failure dicts
    """
    children_by_parent, _ = _relevant_edges(edges, sector_scope, accounting_standard)
    derived: dict[str, tuple[Decimal, str]] = {}
    known = dict(filed)

    def resolve(item_id: str) -> Decimal | None:
        if item_id in known:
            return known[item_id]
        if item_id in derived:
            return derived[item_id][0]
        return None

    violations: list[dict[str, Any]] = []

    # Rollup consistency pass (pre-derivation): checks filed subtotals against all filed children
    for parent_id, child_edges in children_by_parent.items():
        parent_val = filed.get(parent_id)
        if parent_val is None or parent_val == 0:
            continue
        if any(e.child_id not in filed for e in child_edges):
            continue
        expected = sum(_edge_contribution(filed[e.child_id], e.sign) for e in child_edges)
        delta = parent_val - expected
        tolerance = abs(parent_val) * _ROLLUP_TOLERANCE_BP / Decimal("10000")
        if abs(delta) > tolerance:
            check_id = f"rollup:{parent_id}:{sector_scope}:{accounting_standard or ''}"
            violations.append({
                "check_id": check_id,
                "lhs_value": parent_val,
                "rhs_value": expected,
                "delta": delta,
                "delta_bp": delta / parent_val * 10000,
            })

    def _derive_top_down_residuals() -> None:
        """Derive missing residual/catch-all children from known parents.

        This runs before and after bottom-up derivation. The pre-pass is important:
        otherwise a partial child subtotal can be synthesized first and block a
        cleaner derivation from an authoritative parent total.
        """
        changed = True
        while changed:
            changed = False
            for parent_id, child_edges in children_by_parent.items():
                parent_val = resolve(parent_id)
                if parent_val is None:
                    continue
                missing = [e for e in child_edges if resolve(e.child_id) is None]
                if not missing:
                    continue
                residual_missing = [e for e in missing if _is_residual_child(e)]
                if len(residual_missing) == 1:
                    edge = residual_missing[0]
                elif len(missing) == 1:
                    edge = missing[0]
                else:
                    continue
                if edge.sign == 0:
                    continue
                sibling_sum = sum(
                    _edge_contribution(resolve(e.child_id), e.sign)
                    for e in child_edges
                    if e.child_id != edge.child_id and resolve(e.child_id) is not None
                )
                residual = parent_val - sibling_sum
                if edge.sign < 0:
                    residual = -residual
                if edge.child_id not in known and edge.child_id not in derived:
                    derived[edge.child_id] = (residual, "RESIDUAL")
                    known[edge.child_id] = residual
                    changed = True

    # Derive top-down from authoritative filed parents before partial bottom-up
    # subtotals can fill the same slots with incomplete child sums.
    _derive_top_down_residuals()

    # Bottom-up pass: compute intermediates from children
    visiting: set[str] = set()

    def _compute_bottom_up(parent_id: str) -> Decimal | None:
        if resolve(parent_id) is not None:
            return resolve(parent_id)
        if parent_id in visiting:
            return None
        child_edges = children_by_parent.get(parent_id)
        if not child_edges:
            return None

        visiting.add(parent_id)
        known_sum = Decimal("0")
        known_count = 0
        total_count = len(child_edges)
        for e in child_edges:
            child_val = _compute_bottom_up(e.child_id)
            if child_val is not None:
                known_sum += _edge_contribution(child_val, e.sign)
                known_count += 1

        if known_count == 0:
            visiting.discard(parent_id)
            return None
        if known_count == total_count:
            metric_type = "DERIVED_BOTTOM_UP"
        elif Decimal(known_count) / Decimal(total_count) >= _PARTIAL_THRESHOLD:
            metric_type = "DERIVED_PARTIAL"
        else:
            visiting.discard(parent_id)
            return None

        if parent_id not in known and parent_id not in derived:
            derived[parent_id] = (known_sum, metric_type)
        visiting.discard(parent_id)
        return known_sum

    for parent_id in list(children_by_parent):
        _compute_bottom_up(parent_id)

    # Derive residual catch-alls unlocked by bottom-up intermediates.
    _derive_top_down_residuals()

    # Named identity check pass
    for check in identity_checks:
        if str(check["check_id"]).startswith("rollup:"):
            continue
        if check["sector_scope"] not in (sector_scope, "universal"):
            continue
        if check["accounting_standard"] and check["accounting_standard"] != accounting_standard:
            continue
        lhs = resolve(check["lhs_item_id"])
        if lhs is None or lhs == 0:
            continue
        rhs = Decimal("0")
        all_rhs_present = True
        for item_id, sign in zip(check["rhs_item_ids"], check["rhs_signs"]):
            val = resolve(item_id)
            if val is None:
                all_rhs_present = False
                break
            rhs += _edge_contribution(val, sign)
        if not all_rhs_present:
            continue
        delta = lhs - rhs
        tolerance = abs(lhs) * Decimal(check["tolerance_bp"]) / Decimal("10000")
        if abs(delta) > tolerance:
            violations.append({
                "check_id": check["check_id"],
                "lhs_value": lhs,
                "rhs_value": rhs,
                "delta": delta,
                "delta_bp": (delta / lhs * 10000) if lhs else None,
            })

    return derived, violations


def derive_formula_items(
    values: dict[str, Decimal],
    eligible_items: set[str],
    sector_scope: str | None = None,
) -> dict[str, tuple[Decimal, str]]:
    """Compute registry-defined formula line items from already-known values."""
    known = dict(values)
    derived: dict[str, tuple[Decimal, str]] = {}
    if not eligible_items:
        return derived

    for _ in range(len(_L1_FORMULAS)):
        namespace = _with_legacy_aliases({
            key: float(value)
            for key, value in {**known, **{k: v for k, (v, _t) in derived.items()}}.items()
            if value is not None
        })
        changed = False
        for line_item_id, formula in _L1_FORMULAS.items():
            if line_item_id not in eligible_items:
                continue
            if line_item_id in known or line_item_id in derived:
                continue
            if line_item_id == "total_financial_debt" and not any(
                key in namespace
                for key in (
                    "short_term_debt",
                    "long_term_debt_current_portion",
                    "long_term_debt",
                )
            ):
                continue
            if sector_scope == "insurance" and line_item_id == "net_debt":
                formula = "total_financial_debt - (cash_and_cash_equivalents or 0)"
            val = _eval_formula(formula, namespace)
            if val is None:
                continue
            derived[line_item_id] = (Decimal(str(val)), "DERIVED_BOTTOM_UP")
            changed = True
        if not changed:
            break
    return derived


def write_violations(
    violations_by_entity: list[tuple[str, str, int, str, list[dict[str, Any]]]],
) -> int:
    """Persist identity violations to ref_std_identity_violation.

    violations_by_entity: [(entity_id, jurisdiction, fiscal_year, fiscal_period, violations)]
    """
    keys = sorted({
        (entity_id, jurisdiction, fiscal_year, fiscal_period)
        for entity_id, jurisdiction, fiscal_year, fiscal_period, _violations in violations_by_entity
    })
    rows_by_key: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for entity_id, jurisdiction, fiscal_year, fiscal_period, violations in violations_by_entity:
        for v in violations:
            row = (
                entity_id,
                jurisdiction,
                fiscal_year,
                fiscal_period,
                v["check_id"],
                v["lhs_value"],
                v["rhs_value"],
                v["delta"],
                v.get("delta_bp"),
            )
            rows_by_key[row[:5]] = row
    rows = list(rows_by_key.values())
    if not keys:
        return 0
    with connect() as conn, conn.cursor() as cur:
        execute_values(
            cur,
            """
            DELETE FROM ref_std_identity_violation v
             USING (VALUES %s) AS k(entity_id, jurisdiction, fiscal_year, fiscal_period)
             WHERE v.entity_id = k.entity_id
               AND v.jurisdiction = k.jurisdiction
               AND v.fiscal_year = k.fiscal_year::SMALLINT
               AND v.fiscal_period = k.fiscal_period
            """,
            keys,
            page_size=1000,
        )
        if not rows:
            return 0
        return execute_values(
            cur,
            """
            INSERT INTO ref_std_identity_violation
                (entity_id, jurisdiction, fiscal_year, fiscal_period,
                 check_id, lhs_value, rhs_value, delta, delta_bp)
            VALUES %s
            """,
            rows,
            page_size=2000,
        )
