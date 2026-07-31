"""Populate sec.fact_fundamentals_std_jp from context-aware raw JP facts."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics.formulas import _LINE_ITEM_RENAMES
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.state.store import finish_run, start_run, update_run_progress
import psycopg2

from xbrl_sec.sec.std.graph_closure import (
    close_graph,
    derive_formula_items,
    load_edges,
    load_identity_checks,
    validate_hierarchy_graph,
    write_violations,
)
from xbrl_sec.sec.std.m1_aggregation import (
    JPContextStrategy,
    fact_from_jp_row,
    resolve as m1_resolve,
    rule_from_versioned_record,
)
from xbrl_sec.sec.std.versioned_mapping import (
    load_mapping_exceptions,
    load_versioned_mapping,
    select_versioned_mapping,
)


_SHARES_UNITS = frozenset({"shares", "share", "pure"})
_MONETARY_UNITS = frozenset({"USD", "EUR", "JPY"})
_MONETARY_UNIT_TYPES = frozenset({"CCY", "MONETARY", "CURRENCY"})


def _unit_rank(unit_type: str | None, fact_unit: str | None) -> int:
    if str(unit_type or "").upper() not in _MONETARY_UNIT_TYPES:
        return 1
    if not fact_unit or fact_unit.lower() in _SHARES_UNITS:
        return 0
    return 1


def _load_unit_types() -> dict[str, str | None]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT line_item_id, unit_type FROM ref_standardized_line_items")
        return {row[0]: row[1] for row in cur.fetchall()}


_NEGATIVE_LEGACY_LINE_ITEMS = {
    "cogs",
    "rd_expense",
    "sga",
    "interest_expense",
    "tax_provision",
    "capex",
    "dividends_paid",
    "share_repurchases",
}
_NEGATIVE_LINE_ITEMS = {
    _LINE_ITEM_RENAMES.get(item, item) for item in _NEGATIVE_LEGACY_LINE_ITEMS
}


def _canonical_value(line_item_id: str, value: Decimal, multiplier: Decimal) -> Decimal:
    adjusted = value * multiplier
    if line_item_id in _NEGATIVE_LINE_ITEMS:
        return -abs(adjusted)
    return adjusted


def _load_std_paths() -> dict[str, str | None]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT line_item_id, std_concept_path FROM ref_standardized_line_items")
        return {row[0]: row[1] for row in cur.fetchall()}


def _load_formula_line_items() -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT line_item_id
            FROM ref_standardized_line_items
            WHERE COALESCE(is_filed, false) = false
              AND statement_type IN ('income_statement', 'balance_sheet', 'cash_flow_statement', 'derived')
            """
        )
        return {row[0] for row in cur.fetchall()}


def _is_standardizable_dimension(signature: str | None) -> bool:
    if not signature:
        return True
    return "ConsolidatedOrNonConsolidatedAxis" in signature and "Member" in signature


def _fact_fiscal_year(row: dict[str, Any]) -> int:
    if row.get("fiscal_period") in {"FY", "Annual"} and row.get("period_end"):
        return int(row["period_end"].year)
    return int(row["fiscal_year"])


def _annual_rank(row: dict[str, Any]) -> int:
    if row.get("period_start") and row.get("period_end"):
        return 1 if (row["period_end"] - row["period_start"]).days >= 300 else 0
    return 0


def _path_tokens(path: Any) -> list[str]:
    if path is None:
        return []
    if isinstance(path, (list, tuple)):
        return [str(item) for item in path if item is not None]
    text = str(path).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip().strip("'\"") for part in text.split(",") if part.strip()]


def _tier2_group_key(row: dict[str, Any]) -> tuple:
    tokens = _path_tokens(row.get("concept_path"))
    path_parent = tuple(tokens[:-1]) if len(tokens) > 1 else ()
    return (
        row.get("filing_id"),
        row.get("context_id"),
        row.get("dimension_signature"),
        row.get("period_start"),
        row.get("period_end"),
        row.get("unit"),
        path_parent,
    )


def _tier2_context_rank(row: dict[str, Any], line_item: str, unit_type: str | None) -> tuple:
    path_text = " ".join(_path_tokens(row.get("concept_path")))
    income_context = int(
        "IncomeStatement" in path_text
        or "NetIncome" in path_text
        or "ProfitLoss" in path_text
        or "OrdinaryIncome" in path_text
    )
    disclosure_penalty = int(
        any(term in path_text for term in ("Investments", "Tax", "Lease", "DebtAndEquitySecurities"))
        and line_item in {"non_operating_income", "total_operating_expenses"}
    )
    return (
        _unit_rank(unit_type, row.get("unit")),
        1 if row.get("context_tier") == 0 else 0,
        _annual_rank(row),
        income_context,
        -disclosure_penalty,
        row.get("period_end"),
        row.get("filed_date") or row.get("period_end"),
    )


def _merge_tier2_group(items: list[tuple[Decimal, tuple, dict[str, Any], tuple]]) -> tuple:
    total = sum((item[0] for item in items), Decimal("0"))
    anchor = max(items, key=lambda item: item[3])[1]
    metric_type = "T2_SUM" if len(items) > 1 else "T2_COMPONENT"
    source_ids = []
    concept_paths = []
    for _value, out, row, _rank in items:
        if out[9] not in source_ids:
            source_ids.append(out[9])
        if row.get("concept_path") is not None:
            concept_paths.append(str(row["concept_path"]))
    concept_path = " | ".join(concept_paths) if concept_paths else anchor[15]
    return (
        anchor[:6]
        + (
            metric_type,
            total,
            anchor[8],
            ",".join(source_ids),
            anchor[10],
            anchor[11],
            anchor[12],
            anchor[13],
            anchor[14],
            concept_path,
            anchor[16],
            anchor[17],
            anchor[18],
        )
    )


def _hierarchy_sector(row: dict[str, Any]) -> str:
    if row.get("mapping_sector") != "non_bank_financial":
        return row.get("mapping_sector") or "corp"
    sector = str(row.get("gics_sector") or "")
    industry_group = str(row.get("gics_industry_group") or "")
    if sector == "60":
        return "reit"
    if industry_group == "4030":
        return "insurance"
    if industry_group == "4020" or sector == "40":
        return "asset_manager_other_financial"
    return "asset_manager_other_financial"


def _raw_rows(entity_ids: list[str] | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND f.edinet_code = ANY(%s)"
        params.append(entity_ids)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT f.edinet_code, f.concept_id, f.fiscal_year, f.fiscal_period,
                   f.period_start, f.period_end, f.value, f.unit, f.filing_type, f.filed_date,
                   f.filing_id, f.context_id, f.context_tier, f.dimension_signature,
                   f.concept_path, d.mapping_sector,
                   d.gics_sector_code AS gics_sector,
                   d.gics_industry_group_code AS gics_industry_group,
                   NULL::text AS gics_industry,
                   NULL::text AS gics_sub_industry,
                   f.taxonomy AS taxonomy_version,
                   'JP_GAAP'::text AS accounting_standard
            FROM fact_fundamentals_jp f
            LEFT JOIN dim_company_jp d ON d.edinet_code = f.edinet_code
            WHERE f.value_type = 'ORIG'
              AND f.fiscal_year IS NOT NULL
              AND f.fiscal_period IS NOT NULL
              AND f.value IS NOT NULL
              {entity_filter}
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _entities_with_raw() -> list[str]:
    """Return the distinct edinet_codes that have at least one standardizable
    raw row. Used by populate_jp_std to drive entity-chunked processing
    without ever materializing the full raw set in memory.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT f.edinet_code
              FROM fact_fundamentals_jp f
             WHERE f.value_type = 'ORIG'
               AND f.fiscal_year IS NOT NULL
               AND f.fiscal_period IS NOT NULL
               AND f.value IS NOT NULL
             ORDER BY f.edinet_code
            """
        )
        return [row[0] for row in cur.fetchall()]


def _std_is_fresh() -> bool:
    """True iff fact_fundamentals_std_jp is at least as new as the raw table.

    Returns True when std covers all current raw updates (so the work can be
    skipped). NULL handling:
    - Empty raw → nothing to standardize, treated as fresh (skip).
    - Empty std with non-empty raw → not fresh, must run.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT max(updated_at) FROM fact_fundamentals_jp) AS raw_max,
                (SELECT max(updated_at) FROM fact_fundamentals_std_jp) AS std_max
            """
        )
        raw_max, std_max = cur.fetchone()
    if raw_max is None:
        return True
    if std_max is None:
        return False
    return std_max >= raw_max


def _resolve(
    raw_rows: list[dict[str, Any]],
    mapping: dict[str, list[dict[str, Any]]],
    exceptions: dict[str, list[dict[str, Any]]],
    std_paths: dict[str, str | None],
    std_unit_types: dict[str, str | None],
) -> list[tuple]:
    tier1: dict[tuple, tuple[int, tuple]] = {}
    tier2: dict[tuple, list[tuple[Decimal, tuple, dict[str, Any], tuple]]] = defaultdict(list)

    for row in raw_rows:
        if not _is_standardizable_dimension(row["dimension_signature"]):
            continue
        candidates = mapping.get(row["concept_id"]) or []
        exception_candidates = exceptions.get(row["concept_id"]) or []
        if not candidates and not exception_candidates:
            continue
        rec = select_versioned_mapping(
            "JP",
            _hierarchy_sector(row),
            row,
            candidates,
            exception_candidates,
        )
        if rec is None:
            continue
        line_item = rec["target_variable"]
        value = _canonical_value(line_item, row["value"], rec["multiplier"])
        fact_year = _fact_fiscal_year(row)
        key = (row["edinet_code"], fact_year, row["fiscal_period"], line_item)
        out = (
            row["edinet_code"],
            "JP",
            fact_year,
            row["fiscal_period"],
            row["period_end"],
            line_item,
            "RAW" if rec["tier"] == 1 else "T2_SUM",
            value,
            row["unit"],
            row["concept_id"],
            row["filing_type"],
            row["filed_date"],
            row["filing_id"],
            row["context_id"],
            row["dimension_signature"],
            row["concept_path"],
            std_paths.get(line_item),
            rec.get("mapping_id"),
            rec.get("mapping_exception_id"),
        )
        if rec["tier"] == 1:
            rank = (
                _unit_rank(std_unit_types.get(line_item), row["unit"]),
                1 if row["context_tier"] == 0 else 0,
                _annual_rank(row),
                row["period_end"],
                row["filed_date"] or row["period_end"],
            )
            existing = tier1.get(key)
            if existing is None or rank > existing[0]:
                tier1[key] = (rank, out)
        else:
            rank = _tier2_context_rank(row, line_item, std_unit_types.get(line_item))
            tier2[key].append((value, out, row, rank))

    output: list[tuple] = []
    for key in set(tier1) | set(tier2):
        if key in tier1:
            output.append(tier1[key][1])
            continue
        items = tier2[key]
        if not items:
            continue
        by_group: dict[tuple, list[tuple[Decimal, tuple, dict[str, Any], tuple]]] = defaultdict(list)
        for item in items:
            by_group[_tier2_group_key(item[2])].append(item)
        best_group = max(
            by_group.values(),
            key=lambda group: (
                max(item[3] for item in group),
                len(group),
                max(abs(item[0]) for item in group),
            ),
        )
        output.append(_merge_tier2_group(best_group))
    return output


def _resolve_via_m1(
    raw_rows: list[dict[str, Any]],
    mapping: dict[str, list[dict[str, Any]]],
    exceptions: dict[str, list[dict[str, Any]]],
    std_paths: dict[str, str | None],
    std_unit_types: dict[str, str | None],
) -> list[tuple]:
    """Phase 3: route resolution through the shared M:1 resolver."""
    strategy = JPContextStrategy()
    facts_and_rules: list = []
    for row in raw_rows:
        if not _is_standardizable_dimension(row["dimension_signature"]):
            continue
        candidates = mapping.get(row["concept_id"]) or []
        exception_candidates = exceptions.get(row["concept_id"]) or []
        if not candidates and not exception_candidates:
            continue
        rec = select_versioned_mapping(
            "JP",
            _hierarchy_sector(row),
            row,
            candidates,
            exception_candidates,
        )
        if rec is None:
            continue
        rule = rule_from_versioned_record(rec)
        rule = type(rule)(
            mapping_id=rule.mapping_id,
            mapping_exception_id=rule.mapping_exception_id,
            concept_id=str(row["concept_id"]),
            target_variable=rule.target_variable,
            aggregation_type=rule.aggregation_type,
            aggregation_priority=rule.aggregation_priority,
            multiplier=rule.multiplier,
            sign_policy=rule.sign_policy,
            normal_balance=rule.normal_balance,
            tier=rule.tier,
            mapping_source=rule.mapping_source,
        )
        if rule.target_variable in _NEGATIVE_LINE_ITEMS and rule.sign_policy != "force_negative":
            rule = type(rule)(
                mapping_id=rule.mapping_id,
                mapping_exception_id=rule.mapping_exception_id,
                concept_id=rule.concept_id,
                target_variable=rule.target_variable,
                aggregation_type=rule.aggregation_type,
                aggregation_priority=rule.aggregation_priority,
                multiplier=rule.multiplier,
                sign_policy="force_negative",
                normal_balance=rule.normal_balance,
                tier=rule.tier,
                mapping_source=rule.mapping_source,
            )
        fact = fact_from_jp_row(row)
        facts_and_rules.append((fact, rule))

    results = m1_resolve(
        facts_and_rules,
        strategy,
        unit_type_for_target=std_unit_types,
        fact_fiscal_year=lambda f: _fact_fiscal_year({
            "fiscal_period": f.fiscal_period,
            "period_end": f.period_end,
            "fiscal_year": f.fiscal_year,
        }),
    )

    rows: list[tuple] = []
    for res in results:
        # JP rows carry context_id and dimension_signature in the standardized table.
        # context_group_key shape (US-like): (filing, ctx, dim, ...) so positions 1/2 are ctx/dim.
        ctx_id = res.context_group_key[1] if len(res.context_group_key) > 1 else None
        dim_sig = res.context_group_key[2] if len(res.context_group_key) > 2 else None
        rows.append((
            res.entity_id,
            "JP",
            res.fiscal_year,
            res.fiscal_period,
            res.period_end,
            res.target_variable,
            res.metric_type,
            res.value,
            res.unit,
            ",".join(res.source_concept_ids),
            res.filing_type,
            res.filed_date,
            res.filing_id,
            ctx_id,
            dim_sig,
            str(res.concept_path) if res.concept_path is not None else None,
            std_paths.get(res.target_variable),
            res.mapping_ids[0] if res.mapping_ids else None,
            res.mapping_exception_ids[0] if res.mapping_exception_ids else None,
        ))
    return rows


def _run_closure_pass(
    rows: list[tuple],
    raw_rows: list[dict[str, Any]],
    edges: list,
    identity_checks: list,
    std_paths: dict[str, str | None],
    formula_items: set[str],
) -> list[tuple]:
    entity_sector: dict[str, str] = {}
    for r in raw_rows:
        if r["edinet_code"] not in entity_sector and r.get("mapping_sector"):
            entity_sector[r["edinet_code"]] = _hierarchy_sector(r)

    filed_map: dict[tuple, dict[str, Decimal]] = defaultdict(dict)
    period_end_of: dict[tuple, Any] = {}
    currency_of: dict[tuple, str] = {}
    for t in rows:
        k = (t[0], t[2], t[3])
        filed_map[k][t[5]] = t[7]
        if k not in period_end_of:
            period_end_of[k] = t[4]
        if t[8] in _MONETARY_UNITS:
            currency_of[k] = t[8]
        elif k not in currency_of and t[8]:
            currency_of[k] = t[8]

    extra: list[tuple] = []
    violations_all = []
    for (entity_id, fy, fp), filed in filed_map.items():
        sector_scope = entity_sector.get(entity_id, "corp")
        derived, violations = close_graph(filed, edges, identity_checks, sector_scope, "JP_GAAP")
        formula_input = {**filed, **{line_item_id: val for line_item_id, (val, _mtype) in derived.items()}}
        derived.update(derive_formula_items(formula_input, formula_items, sector_scope=sector_scope))
        period_end = period_end_of.get((entity_id, fy, fp))
        currency = currency_of.get((entity_id, fy, fp))
        for line_item_id, (val, mtype) in derived.items():
            extra.append((
                entity_id, "JP", fy, fp, period_end,
                line_item_id, mtype, val, currency,
                None, None, None, None,
                None, None,  # context_id, dimension_signature
                None,        # concept_path
                std_paths.get(line_item_id), None, None,
            ))
        violations_all.append((entity_id, "JP", fy, fp, violations))
    write_violations(violations_all)
    return extra


# Chunk size for entity-batched standardization. Tuned to keep one chunk's
# raw rows + resolved rows under ~3-4 GB resident on the JP scope, where the
# average entity has ~10k raw rows. Set lower if you OOM; higher to reduce
# DB round-trip overhead.
_STANDARDIZE_CHUNK_SIZE = 200

_STD_INSERT_SQL = """
    INSERT INTO fact_fundamentals_std_jp
        (edinet_code, jurisdiction, fiscal_year, fiscal_period, period_end,
         line_item_id, metric_type, value, currency, source_concept_id,
         filing_form, filed_date, filing_id, context_id, dimension_signature,
         concept_path, std_concept_path, mapping_id, mapping_exception_id)
    VALUES %s
    ON CONFLICT (edinet_code, jurisdiction, fiscal_year, fiscal_period, line_item_id)
    DO UPDATE SET
        period_end = EXCLUDED.period_end,
        metric_type = EXCLUDED.metric_type,
        value = EXCLUDED.value,
        currency = EXCLUDED.currency,
        source_concept_id = EXCLUDED.source_concept_id,
        filing_form = EXCLUDED.filing_form,
        filed_date = EXCLUDED.filed_date,
        filing_id = EXCLUDED.filing_id,
        context_id = EXCLUDED.context_id,
        dimension_signature = EXCLUDED.dimension_signature,
        concept_path = EXCLUDED.concept_path,
        std_concept_path = EXCLUDED.std_concept_path,
        mapping_id = EXCLUDED.mapping_id,
        mapping_exception_id = EXCLUDED.mapping_exception_id,
        updated_at = now()
"""


def populate_jp_std(
    entity_ids: list[str] | None = None,
    full: bool = False,
    chunk_size: int = _STANDARDIZE_CHUNK_SIZE,
) -> int:
    """Standardize JP raw facts into fact_fundamentals_std_jp.

    Entity-chunked to bound memory: each chunk fetches its raw rows, resolves,
    runs the closure pass, and upserts before the next chunk starts. Releases
    per-chunk allocations explicitly so Python can return memory to the OS.

    Semantics match the previous monolithic version:

    - `_resolve` and `_run_closure_pass` are keyed by edinet_code, so chunking
      across entities does not change any output row.
    - `full=True` with no entity_ids → TRUNCATE the table once up front, then
      upsert each chunk. (Old behavior: TRUNCATE then one big INSERT.)
    - `entity_ids` set → DELETE those entities once up front, then upsert each
      chunk. (Old behavior: DELETE then one big INSERT, restricted to those.)
    - Neither flag set → no delete; rely on ON CONFLICT DO UPDATE. (Same as
      before.)
    """
    ctx = start_run("JP", "standardize", "full_refresh" if full else "incremental")
    try:
        # Freshness short-circuit: only applies to a default-scoped incremental
        # call (no entity scope, not a full rebuild). An explicit scope or full
        # flag still forces the work to run, mirroring sync_xbrl_index's
        # force_resync semantics.
        if not full and not entity_ids and _std_is_fresh():
            print("JP standardize: skipped (fact_fundamentals_std_jp already covers raw)", flush=True)
            finish_run(ctx, "succeeded", rows_in=0, rows_out=0)
            return 0
        mapping = load_versioned_mapping("JP")
        exceptions = load_mapping_exceptions("JP")
        std_paths = _load_std_paths()
        std_unit_types = _load_unit_types()
        formula_items = _load_formula_line_items()
        try:
            validate_hierarchy_graph()
            edges = load_edges()
            identity_checks = load_identity_checks()
        except psycopg2.errors.UndefinedTable:
            edges = []
            identity_checks = {}

        # One-time deletion up front. After this, each chunk's upsert relies
        # on ON CONFLICT DO UPDATE for replacement semantics.
        if full and not entity_ids:
            with connect() as conn, conn.cursor() as cur:
                cur.execute("TRUNCATE fact_fundamentals_std_jp")
        elif entity_ids:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM fact_fundamentals_std_jp WHERE edinet_code = ANY(%s)",
                    (list(entity_ids),),
                )

        # Determine the entities we actually need to process.
        if entity_ids:
            scope = list(entity_ids)
        else:
            scope = _entities_with_raw()

        use_m1 = load_settings().use_m1_resolver
        total_raw = 0
        total_written = 0

        for chunk_start in range(0, len(scope), chunk_size):
            chunk = scope[chunk_start : chunk_start + chunk_size]
            raw = _raw_rows(chunk)
            if not raw:
                continue
            total_raw += len(raw)

            if use_m1:
                rows = _resolve_via_m1(raw, mapping, exceptions, std_paths, std_unit_types)
            else:
                rows = _resolve(raw, mapping, exceptions, std_paths, std_unit_types)
            if edges:
                rows.extend(_run_closure_pass(rows, raw, edges, identity_checks, std_paths, formula_items))

            if rows:
                with connect() as conn, conn.cursor() as cur:
                    written = execute_values(cur, _STD_INSERT_SQL, rows, page_size=5000)
                total_written += written

            update_run_progress(ctx, rows_in=total_raw, rows_out=total_written)
            # Explicit drop so the next chunk's allocations don't pile on top.
            del raw, rows

        finish_run(ctx, "succeeded", rows_in=total_raw, rows_out=total_written)
        return total_written
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
