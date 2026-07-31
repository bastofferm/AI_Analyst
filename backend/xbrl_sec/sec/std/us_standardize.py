"""Populate sec.fact_fundamentals_std_us from SEC companyfacts raw facts."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg2

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics.formulas import _LINE_ITEM_RENAMES
from xbrl_sec.sec.settings import load_settings
from xbrl_sec.sec.state.store import finish_run, start_run
from xbrl_sec.sec.std.graph_closure import (
    close_graph,
    derive_formula_items,
    load_edges,
    load_identity_checks,
    validate_hierarchy_graph,
    write_violations,
)
from xbrl_sec.sec.std.m1_aggregation import (
    USContextStrategy,
    fact_from_us_row,
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


def _fact_fiscal_year(row: dict[str, Any]) -> int:
    # Annual: derive from period_end (the SEC `fy` is the filing year, which shifts on
    # 10-K comparatives — see migration 112). Quarterly rows are period-aligned upstream
    # by _period_align_quarterly_fiscal_year (which overwrites row["fiscal_year"]), so the
    # fall-through here already returns the corrected, period-aligned quarter year.
    if row.get("fiscal_period") in {"FY", "Annual"} and row.get("period_end"):
        return int(row["period_end"].year)
    return int(row["fiscal_year"])


def _period_align_quarterly_fiscal_year(raw_rows: list[dict[str, Any]]) -> int:
    """Overwrite the filing-context ``fiscal_year`` on QUARTERLY raw rows with a
    period-aligned fiscal year derived from ``period_end`` + the company's fiscal
    year-end month.

    Why: the SEC companyfacts ``fy`` is the fiscal year of the FILING, not the period.
    A prior quarter reported as a comparative in a later 10-Q/10-K inherits the later
    filing's ``fy``. Because the std unique key is
    ``(cik, fiscal_year, fiscal_period, line_item_id)``, two *different* real quarters
    (e.g. Dec-2023 and Dec-2024, both surfacing as ``Q1``) then collide and one
    overwrites the other — silently dropping and mis-dating recent quarters. Annual
    rows are already period-aligned by ``_fact_fiscal_year`` and are left untouched.

    The fiscal year a quarter belongs to is the FY it closes into: ``year+1`` when the
    quarter ends after the company's fiscal year-end month, else ``year`` (this reduces
    to ``period_end.year`` for December filers and is correct for early/late FYE filers
    like WMT/AAPL). Mutates ``raw_rows`` in place; no-op for a CIK with no annual anchor.
    Returns the number of quarterly rows corrected (for logging/verification).
    """
    fye_counts: dict[str, Counter] = defaultdict(Counter)
    for r in raw_rows:
        if r.get("fiscal_period") in {"FY", "Annual"} and r.get("period_end") is not None:
            fye_counts[r["cik"]][r["period_end"].month] += 1
    fye_month = {cik: c.most_common(1)[0][0] for cik, c in fye_counts.items() if c}

    corrected = 0
    for r in raw_rows:
        fp = r.get("fiscal_period")
        pe = r.get("period_end")
        if fp in {"FY", "Annual"} or pe is None:
            continue
        m = fye_month.get(r["cik"])
        if m is None:
            continue  # no annual anchor for this filer — leave the filing-context year
        aligned = pe.year + 1 if pe.month > m else pe.year
        if r.get("fiscal_year") != aligned:
            r["fiscal_year"] = aligned
            corrected += 1
    return corrected


def _annual_rank(row: dict[str, Any]) -> int:
    if row.get("period_start") and row.get("period_end"):
        return 1 if (row["period_end"] - row["period_start"]).days >= 300 else 0
    return 0


def _presentation_rank(row: dict[str, Any]) -> int:
    level = row.get("pre_level")
    if level is None:
        return -999
    return -int(level)


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
        row.get("period_start"),
        row.get("period_end"),
        row.get("unit"),
        row.get("statement_type"),
        row.get("pre_parent_id") or path_parent,
    )


def _tier2_context_rank(row: dict[str, Any], line_item: str, unit_type: str | None) -> tuple:
    parent = str(row.get("pre_parent_id") or "")
    path_text = " ".join(_path_tokens(row.get("concept_path")))
    income_context = int(
        row.get("statement_type") == "IncomeStatement"
        or "IncomeStatement" in parent
        or "IncomeStatement" in path_text
        or "NetIncomeLoss" in path_text
        or "IncomeLossFromContinuingOperations" in path_text
    )
    disclosure_penalty = int(
        any(term in parent for term in ("Investments", "Tax", "Lease", "DebtAndEquitySecurities"))
        and line_item in {"non_operating_income", "total_operating_expenses"}
    )
    return (
        _unit_rank(unit_type, row.get("unit")),
        _annual_rank(row),
        income_context,
        -disclosure_penalty,
        _presentation_rank(row),
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
    concept_path = " | ".join(concept_paths) if concept_paths else anchor[13]
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
            concept_path,
            anchor[14],
            anchor[15],
            anchor[16],
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


def _raw_rows(entity_ids: list[str] | None = None, as_of: date | None = None) -> list[dict[str, Any]]:
    """Latest-vintage raw facts for standardization.

    The raw table is bitemporal (one row per filing × period; see migration 113),
    so we pick a single vintage per (cik, concept_id, period_end, fiscal_period)
    via DISTINCT ON, ordered by filed_date DESC. Default (as_of=None) is the
    latest-known view. Passing as_of restricts to facts filed on/before that date,
    yielding a point-in-time standardization free of look-ahead bias.
    """
    params: list[Any] = []
    entity_filter = ""
    if entity_ids:
        entity_filter = "AND f.cik = ANY(%s)"
        params.append(entity_ids)
    as_of_filter = ""
    if as_of is not None:
        as_of_filter = "AND f.filed_date IS NOT NULL AND f.filed_date <= %s"
        params.append(as_of)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (f.cik, f.concept_id, f.period_end, f.fiscal_period)
                   f.cik, f.concept_id, f.fiscal_year, f.fiscal_period,
                   f.period_start, f.period_end, f.value, f.unit, f.filing_type, f.filed_date,
                   f.filing_id, f.concept_path, f.pre_level, f.pre_position, d.mapping_sector,
                   d.gics_sector_code AS gics_sector,
                   d.gics_industry_group_code AS gics_industry_group,
                   NULL::text AS gics_industry,
                   NULL::text AS gics_sub_industry,
                   f.taxonomy AS taxonomy_version,
                   'US_GAAP'::text AS accounting_standard
            FROM fact_fundamentals_us f
            LEFT JOIN dim_company_us d ON d.cik = f.cik
            WHERE f.value_type = 'ORIG'
              AND f.fiscal_year IS NOT NULL
              AND f.fiscal_period IS NOT NULL
              AND f.value IS NOT NULL
              {entity_filter}
              {as_of_filter}
            ORDER BY f.cik, f.concept_id, f.period_end, f.fiscal_period,
                     f.filed_date DESC NULLS LAST, f.filing_id DESC
            """,
            params,
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _resolve(
    raw_rows: list[dict[str, Any]],
    mapping: dict[str, list[dict[str, Any]]],
    exceptions: dict[str, list[dict[str, Any]]],
    std_paths: dict[str, str | None],
    std_unit_types: dict[str, str | None],
) -> list[tuple]:
    tier1: dict[tuple, tuple[tuple, tuple]] = {}
    tier2: dict[tuple, list[tuple[Decimal, tuple, dict[str, Any], tuple]]] = defaultdict(list)

    for row in raw_rows:
        candidates = mapping.get(row["concept_id"]) or []
        exception_candidates = exceptions.get(row["concept_id"]) or []
        if not candidates and not exception_candidates:
            continue
        rec = select_versioned_mapping(
            "US",
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
        key = (row["cik"], fact_year, row["fiscal_period"], line_item)
        out = (
            row["cik"],
            "US",
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
            row["concept_path"],
            std_paths.get(line_item),
            rec.get("mapping_id"),
            rec.get("mapping_exception_id"),
        )
        if rec["tier"] == 1:
            rank = (
                _unit_rank(std_unit_types.get(line_item), row["unit"]),
                _annual_rank(row),
                _presentation_rank(row),
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
    strategy = USContextStrategy()
    facts_and_rules: list = []
    raw_by_concept: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        candidates = mapping.get(row["concept_id"]) or []
        exception_candidates = exceptions.get(row["concept_id"]) or []
        if not candidates and not exception_candidates:
            continue
        rec = select_versioned_mapping(
            "US",
            _hierarchy_sector(row),
            row,
            candidates,
            exception_candidates,
        )
        if rec is None:
            continue
        rule = rule_from_versioned_record(rec)
        # rule_from_versioned_record fills target/aggregation/sign; attach concept_id.
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
        fact = fact_from_us_row(row)
        # Canonical negative line items are still forced negative at write time
        # via the legacy _canonical_value rule; preserve that by overriding
        # sign_policy when the target is in the negative-canonical set.
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
        facts_and_rules.append((fact, rule))
        raw_by_concept.setdefault(rule.concept_id, row)

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
        # Match the legacy tuple shape expected by the US insert path.
        if res.metric_type == "RAW":
            metric_type = "RAW"
        else:
            metric_type = res.metric_type  # T2_SUM or T2_COMPONENT
        rows.append((
            res.entity_id,
            "US",
            res.fiscal_year,
            res.fiscal_period,
            res.period_end,
            res.target_variable,
            metric_type,
            res.value,
            res.unit,
            ",".join(res.source_concept_ids),
            res.filing_type,
            res.filed_date,
            res.filing_id,
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
        if r["cik"] not in entity_sector and r.get("mapping_sector"):
            entity_sector[r["cik"]] = _hierarchy_sector(r)

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
        derived, violations = close_graph(filed, edges, identity_checks, sector_scope, "US_GAAP")
        # Guards for the 111_cogs_residual_edges child set: that rollup exists
        # only so the top-down pass can derive cost_of_goods_sold residually.
        # A total synthesized from those children is unreliable (a partial sum
        # missing cogs understates badly), and cogs is an expense, so a
        # non-negative residual signals a non-conforming statement structure.
        derived.pop("total_cost_and_expenses", None)
        cogs = derived.get("cost_of_goods_sold")
        if cogs is not None and cogs[1] == "RESIDUAL" and cogs[0] >= 0:
            derived.pop("cost_of_goods_sold")
        formula_input = {**filed, **{line_item_id: val for line_item_id, (val, _mtype) in derived.items()}}
        derived.update(derive_formula_items(formula_input, formula_items, sector_scope=sector_scope))
        period_end = period_end_of.get((entity_id, fy, fp))
        currency = currency_of.get((entity_id, fy, fp))
        for line_item_id, (val, mtype) in derived.items():
            extra.append((
                entity_id, "US", fy, fp, period_end,
                line_item_id, mtype, val, currency,
                None, None, None, None, None,
                std_paths.get(line_item_id), None, None,
            ))
        violations_all.append((entity_id, "US", fy, fp, violations))
    write_violations(violations_all)
    return extra


def populate_us_std(entity_ids: list[str] | None = None, full: bool = False, as_of: date | None = None) -> int:
    """Standardize US raw facts. as_of (filed_date cutoff) yields a point-in-time
    rebuild; default None uses the latest-known vintage per period."""
    ctx = start_run("US", "standardize", "full_refresh" if full else "incremental")
    try:
        mapping = load_versioned_mapping("US")
        exceptions = load_mapping_exceptions("US")
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
        raw = _raw_rows(entity_ids, as_of=as_of)
        # Period-align quarterly fiscal_year before resolution so comparatives don't
        # collide on the std key and drop real quarters (see the function docstring).
        _period_align_quarterly_fiscal_year(raw)
        if load_settings().use_m1_resolver:
            rows = _resolve_via_m1(raw, mapping, exceptions, std_paths, std_unit_types)
        else:
            rows = _resolve(raw, mapping, exceptions, std_paths, std_unit_types)
        if edges:
            rows.extend(_run_closure_pass(rows, raw, edges, identity_checks, std_paths, formula_items))
        if full or entity_ids:
            with connect() as conn, conn.cursor() as cur:
                if entity_ids:
                    cur.execute("DELETE FROM fact_fundamentals_std_us WHERE cik = ANY(%s)", (entity_ids,))
                else:
                    cur.execute("TRUNCATE fact_fundamentals_std_us")
        if rows:
            sql = """
                INSERT INTO fact_fundamentals_std_us
                    (cik, jurisdiction, fiscal_year, fiscal_period, period_end,
                     line_item_id, metric_type, value, currency, source_concept_id,
                     filing_form, filed_date, filing_id, concept_path, std_concept_path,
                     mapping_id, mapping_exception_id)
                VALUES %s
                ON CONFLICT (cik, jurisdiction, fiscal_year, fiscal_period, line_item_id)
                DO UPDATE SET
                    period_end = EXCLUDED.period_end,
                    metric_type = EXCLUDED.metric_type,
                    value = EXCLUDED.value,
                    currency = EXCLUDED.currency,
                    source_concept_id = EXCLUDED.source_concept_id,
                    filing_form = EXCLUDED.filing_form,
                    filed_date = EXCLUDED.filed_date,
                    filing_id = EXCLUDED.filing_id,
                    concept_path = EXCLUDED.concept_path,
                    std_concept_path = EXCLUDED.std_concept_path,
                    mapping_id = EXCLUDED.mapping_id,
                    mapping_exception_id = EXCLUDED.mapping_exception_id,
                    updated_at = now()
            """
            with connect() as conn, conn.cursor() as cur:
                written = execute_values(cur, sql, rows, page_size=5000)
        else:
            written = 0
        finish_run(ctx, "succeeded", rows_in=len(raw), rows_out=written)
        return written
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
