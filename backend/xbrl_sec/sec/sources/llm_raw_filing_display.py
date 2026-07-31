"""LLM-curated display specs over persisted filing-native statement rows.

The module intentionally treats the filing-native rows as the source of truth.
The LLM emits layout instructions only; values are computed locally from
stored source rows before the projection is persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Literal

from pydantic import ValidationError

from xbrl_sec.llm.callbacks import setup_llm_cache
from xbrl_sec.llm.schemas.sec import (
    RawFilingDisplayRowSpec,
    RawFilingDisplaySpec,
    RawFilingStatementDisplaySpec,
)
from xbrl_sec.sec.db.connection import connect


SCHEMA_VERSION = "raw_filing_display_schema_v1"
DEFAULT_MODEL = "deepseek-v4-flash"
PROMPT_VERSION = "raw_filing_display_v1"
_STATEMENT_TYPES = {
    "BS": "balance_sheet",
    "IS": "income_statement",
    "CF": "cash_flow",
}


@dataclass(frozen=True)
class SourceColumn:
    column_key: str
    label: str
    column_kind: str
    period_start: Any
    period_end: Any
    fiscal_year: int | None
    fiscal_period: str | None
    column_order: int


@dataclass
class SourceRow:
    node_key: str
    parent_node_key: str | None
    source_concept_id: str
    display_label: str
    raw_label: str | None
    display_role: str
    default_visibility: str
    display_depth: int
    display_order: int
    values: dict[str, Decimal | None]
    units: dict[str, str | None]


@dataclass
class SourceStatement:
    statement_display_id: int
    jurisdiction: str
    entity_id: str
    ticker: str | None
    filing_id: str
    filing_form: str | None
    filed_date: Any
    fiscal_year: int | None
    fiscal_period: str | None
    period_end: Any
    accounting_standard: str
    api_statement: str
    statement_type: str
    statement_title: str
    role_uri: str
    columns: list[SourceColumn]
    rows: list[SourceRow]


def _dump_model(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return dict(model)


def _pg_json(value: Any):
    from psycopg2.extras import Json

    return Json(value)


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _fingerprint(packet: dict[str, Any]) -> str:
    raw = json.dumps(packet, sort_keys=True, default=_json_default, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:96] or fallback


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _resolve_source_statements(
    cur,
    *,
    jurisdiction: str,
    entity_id: str | None = None,
    ticker: str | None = None,
    filing_id: str | None = None,
    fiscal_period: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    statements: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    stmt_filter = sorted({s.upper() for s in (statements or _STATEMENT_TYPES)})
    params: list[Any] = [jurisdiction, stmt_filter]
    where = ["jurisdiction = %s", "api_statement = ANY(%s)"]
    if entity_id:
        params.append(entity_id)
        where.append("entity_id = %s")
    if ticker:
        params.append(ticker)
        where.append("upper(ticker) = upper(%s)")
    if filing_id:
        params.append(filing_id)
        where.append("filing_id = %s")
    if fiscal_period:
        params.append(fiscal_period)
        where.append("fiscal_period = %s")
    if year_min is not None:
        params.append(year_min)
        where.append("fiscal_year >= %s")
    if year_max is not None:
        params.append(year_max)
        where.append("fiscal_year <= %s")

    where_sql = " AND ".join(where)
    cur.execute(
        f"""
        WITH candidate AS (
            SELECT filing_id
            FROM fact_filing_statement_display
            WHERE {where_sql}
            GROUP BY filing_id
            ORDER BY MAX(fiscal_year) DESC NULLS LAST,
                     MAX(filed_date) DESC NULLS LAST,
                     filing_id DESC
            LIMIT 1
        )
        SELECT statement_display_id, jurisdiction, entity_id, ticker, filing_id,
               filing_form, filed_date, fiscal_year, fiscal_period, period_end,
               accounting_standard, api_statement, statement_type, statement_title,
               role_uri
        FROM fact_filing_statement_display
        WHERE filing_id = (SELECT filing_id FROM candidate)
          AND jurisdiction = %s
          AND api_statement = ANY(%s)
        ORDER BY CASE api_statement WHEN 'BS' THEN 1 WHEN 'IS' THEN 2 ELSE 3 END
        """,
        params + [jurisdiction, stmt_filter],
    )
    cols = [desc.name for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_source_statement(cur, row: dict[str, Any]) -> SourceStatement:
    sid = int(row["statement_display_id"])
    cur.execute(
        """
        SELECT column_key, label, column_kind, period_start, period_end,
               fiscal_year, fiscal_period, column_order
        FROM fact_filing_statement_display_column
        WHERE statement_display_id = %s
        ORDER BY column_order
        """,
        (sid,),
    )
    columns = [
        SourceColumn(
            column_key=str(r[0]),
            label=str(r[1]),
            column_kind=str(r[2]),
            period_start=r[3],
            period_end=r[4],
            fiscal_year=int(r[5]) if r[5] is not None else None,
            fiscal_period=str(r[6]) if r[6] is not None else None,
            column_order=int(r[7]),
        )
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT n.node_key, n.parent_node_key, n.source_concept_id, n.display_label,
               n.raw_label, n.display_role, n.default_visibility, n.display_depth,
               n.display_order, v.column_key, v.value, v.unit
        FROM fact_filing_statement_display_node n
        LEFT JOIN fact_filing_statement_display_value v ON v.node_id = n.node_id
        WHERE n.statement_display_id = %s
          AND n.default_visibility <> 'hidden'
        ORDER BY n.display_order, v.column_key
        """,
        (sid,),
    )
    grouped: dict[str, SourceRow] = {}
    for raw in cur.fetchall():
        node_key = str(raw[0])
        if node_key not in grouped:
            grouped[node_key] = SourceRow(
                node_key=node_key,
                parent_node_key=str(raw[1]) if raw[1] else None,
                source_concept_id=str(raw[2]),
                display_label=str(raw[3]),
                raw_label=str(raw[4]) if raw[4] else None,
                display_role=str(raw[5] or ""),
                default_visibility=str(raw[6] or "default"),
                display_depth=int(raw[7] or 0),
                display_order=int(raw[8] or 0),
                values={},
                units={},
            )
        if raw[9] is not None:
            grouped[node_key].values[str(raw[9])] = _to_decimal(raw[10])
            grouped[node_key].units[str(raw[9])] = str(raw[11]) if raw[11] else None

    return SourceStatement(
        statement_display_id=sid,
        jurisdiction=str(row["jurisdiction"]),
        entity_id=str(row["entity_id"]),
        ticker=str(row["ticker"]) if row.get("ticker") else None,
        filing_id=str(row["filing_id"]),
        filing_form=str(row["filing_form"]) if row.get("filing_form") else None,
        filed_date=row.get("filed_date"),
        fiscal_year=int(row["fiscal_year"]) if row.get("fiscal_year") is not None else None,
        fiscal_period=str(row["fiscal_period"]) if row.get("fiscal_period") else None,
        period_end=row.get("period_end"),
        accounting_standard=str(row["accounting_standard"]),
        api_statement=str(row["api_statement"]),
        statement_type=str(row["statement_type"]),
        statement_title=str(row["statement_title"]),
        role_uri=str(row["role_uri"]),
        columns=columns,
        rows=list(grouped.values()),
    )


def _statement_packet(stmt: SourceStatement, *, row_limit: int = 120) -> dict[str, Any]:
    columns = [
        {
            "key": c.column_key,
            "label": c.label,
            "kind": c.column_kind,
            "fiscal_year": c.fiscal_year,
            "fiscal_period": c.fiscal_period,
        }
        for c in stmt.columns
    ]
    rows = []
    for row in sorted(stmt.rows, key=lambda r: r.display_order)[:row_limit]:
        rows.append(
            {
                "source_node_key": row.node_key,
                "parent_node_key": row.parent_node_key,
                "source_concept_id": row.source_concept_id,
                "label": row.raw_label or row.display_label,
                "display_role": row.display_role,
                "display_depth": row.display_depth,
                "values": {
                    key: (float(value) if value is not None else None)
                    for key, value in row.values.items()
                },
            }
        )
    return {
        "api_statement": stmt.api_statement,
        "statement_type": stmt.statement_type,
        "statement_title": stmt.statement_title,
        "role_uri": stmt.role_uri,
        "columns": columns,
        "rows": rows,
    }


def build_input_packet(statements: list[SourceStatement]) -> dict[str, Any]:
    if not statements:
        raise ValueError("No persisted filing-native statement rows found.")
    first = statements[0]
    return {
        "jurisdiction": first.jurisdiction,
        "entity_id": first.entity_id,
        "ticker": first.ticker,
        "filing_id": first.filing_id,
        "filing_form": first.filing_form,
        "filed_date": first.filed_date.isoformat() if first.filed_date else None,
        "fiscal_year": first.fiscal_year,
        "fiscal_period": first.fiscal_period,
        "instructions": {
            "no_numeric_output": True,
            "default_depth": 1,
            "detail_depth": 2,
            "allowed_aggregations": ["direct", "sum", "subtract", "none"],
        },
        "statements": [_statement_packet(stmt) for stmt in statements],
    }


def _heuristic_spec(packet: dict[str, Any]) -> RawFilingDisplaySpec:
    specs: list[RawFilingStatementDisplaySpec] = []
    for stmt in packet.get("statements") or []:
        rows = stmt.get("rows") or []
        included = [
            row for row in rows
            if int(row.get("display_depth") or 0) <= 2
        ]
        parent_included = {row["source_node_key"] for row in included}
        spec_rows: list[RawFilingDisplayRowSpec] = []
        used_keys: set[str] = set()
        for idx, row in enumerate(included):
            depth = max(0, min(int(row.get("display_depth") or 0), 2))
            base_key = _slug(str(row.get("label") or row.get("source_node_key") or ""), f"row_{idx + 1}")
            row_key = base_key
            n = 2
            while row_key in used_keys:
                row_key = f"{base_key}_{n}"
                n += 1
            used_keys.add(row_key)
            parent_source = row.get("parent_node_key")
            parent_key = None
            if parent_source in parent_included:
                for prior in spec_rows:
                    if parent_source in prior.source_node_keys:
                        parent_key = prior.row_key
                        break
            is_section = depth == 0 and not any(v is not None for v in (row.get("values") or {}).values())
            spec_rows.append(
                RawFilingDisplayRowSpec(
                    row_key=row_key,
                    parent_row_key=parent_key,
                    display_label=str(row.get("label") or row_key).strip()[:120],
                    row_kind="section" if is_section else "detail",
                    aggregation="none" if is_section else "direct",
                    visibility="default" if depth <= 1 else "detail",
                    depth=depth,
                    source_node_keys=[] if is_section else [str(row["source_node_key"])],
                    confidence=0.5,
                    rationale="Heuristic fallback from filing presentation hierarchy.",
                )
            )
        specs.append(
            RawFilingStatementDisplaySpec(
                statement_type=_STATEMENT_TYPES.get(str(stmt.get("api_statement")), "income_statement"),  # type: ignore[arg-type]
                api_statement=str(stmt.get("api_statement")),  # type: ignore[arg-type]
                display_title=str(stmt.get("statement_title") or "Financial statement"),
                rows=spec_rows,
            )
        )
    return RawFilingDisplaySpec(statements=specs, summary="Heuristic filing presentation display spec.")


def _call_llm(
    packet: dict[str, Any],
    *,
    model: str,
    llm: Any | None = None,
) -> RawFilingDisplaySpec:
    from langchain_core.messages import HumanMessage

    from xbrl_sec.llm import ChatDeepSeek
    from xbrl_sec.llm.prompts.raw_filing_display import RAW_FILING_DISPLAY_PROMPT

    setup_llm_cache()
    # This model spends a large, size-dependent share of max_tokens on internal
    # reasoning before emitting the structured JSON tool call; a filing with
    # dozens of rows needs far more headroom than a short chat reply would.
    chat = llm or ChatDeepSeek(model=model, temperature=0.1, max_tokens=20000)
    structured = chat.with_structured_output(RawFilingDisplaySpec)
    packet_json = json.dumps(packet, ensure_ascii=False, default=_json_default)
    messages = RAW_FILING_DISPLAY_PROMPT.format_messages(packet_json=packet_json)
    try:
        result = structured.invoke(messages)
    except ValidationError as exc:
        # The model occasionally overshoots a field's max_length (e.g. `summary`,
        # `rationale`) even though the JSON schema states the limit. Give it one
        # chance to self-correct before giving up.
        retry_messages = messages + [
            HumanMessage(
                content=(
                    "Your previous response violated the output schema's field "
                    "constraints. Return a corrected spec that satisfies every "
                    f"field's constraints (e.g. max length). Errors: {exc}"
                )
            )
        ]
        result = structured.invoke(retry_messages)
    spec = result if isinstance(result, RawFilingDisplaySpec) else RawFilingDisplaySpec.model_validate(result)
    spec = _normalize_spec_visibility(spec)
    errors = _validation_errors(spec, packet)
    if errors:
        retry_messages = messages + [
            HumanMessage(
                content=(
                    "The display spec failed validation. Return a corrected spec only. "
                    "Validation errors: " + json.dumps(errors[:20], ensure_ascii=False, default=str)
                )
            )
        ]
        result = structured.invoke(retry_messages)
        spec = result if isinstance(result, RawFilingDisplaySpec) else RawFilingDisplaySpec.model_validate(result)
        spec = _normalize_spec_visibility(spec)
    return spec


def _validation_errors(spec: RawFilingDisplaySpec, packet: dict[str, Any]) -> list[dict[str, Any]]:
    source_by_statement = {
        str(stmt["api_statement"]): {str(row["source_node_key"]) for row in (stmt.get("rows") or [])}
        for stmt in (packet.get("statements") or [])
    }
    errors: list[dict[str, Any]] = []
    for stmt in spec.statements:
        available_sources = source_by_statement.get(stmt.api_statement, set())
        row_keys = [row.row_key for row in stmt.rows]
        duplicates = [key for key, count in Counter(row_keys).items() if count > 1]
        for key in duplicates:
            errors.append({"key": "duplicate_row_key", "statement": stmt.api_statement, "row_key": key})
        row_key_set = set(row_keys)
        for row in stmt.rows:
            if row.parent_row_key and row.parent_row_key not in row_key_set:
                errors.append({
                    "key": "unknown_parent_row",
                    "statement": stmt.api_statement,
                    "row_key": row.row_key,
                    "parent_row_key": row.parent_row_key,
                })
            unknown = [key for key in row.source_node_keys if key not in available_sources]
            if unknown:
                errors.append({
                    "key": "unknown_source_node",
                    "statement": stmt.api_statement,
                    "row_key": row.row_key,
                    "source_node_keys": unknown,
                })
            if row.aggregation != "none" and not row.source_node_keys:
                errors.append({
                    "key": "aggregation_without_sources",
                    "statement": stmt.api_statement,
                    "row_key": row.row_key,
                    "aggregation": row.aggregation,
                })
            if row.aggregation == "none" and row.source_node_keys:
                errors.append({
                    "key": "none_aggregation_with_sources",
                    "statement": stmt.api_statement,
                    "row_key": row.row_key,
                })
    return errors


def _normalize_spec_visibility(spec: RawFilingDisplaySpec) -> RawFilingDisplaySpec:
    """Apply product-level depth rules to the LLM layout.

    The model is allowed to choose labels/grouping, but the UI contract is
    clearer: depth-2+ is detail, while depth-0/1 structural rows are default.
    A common LLM mistake is making a section row default and its only numeric
    child detail, which hides the important value in the compact view.
    """
    for statement in spec.statements:
        by_key = {row.row_key: row for row in statement.rows}
        children: dict[str, list[RawFilingDisplayRowSpec]] = defaultdict(list)
        for row in statement.rows:
            row_kind = str(row.row_kind or "").strip().lower().replace("-", "_")
            aggregation = str(row.aggregation or "").strip().lower().replace("-", "_")
            visibility = str(row.visibility or "").strip().lower().replace("-", "_")

            if row_kind in {"line", "line_item", "component", "direct", "reported"}:
                row_kind = "detail"
            elif row_kind in {"group", "header"}:
                row_kind = "section"
            elif row_kind not in {"detail", "subtotal", "total", "section"}:
                row_kind = "section" if not row.source_node_keys else "detail"

            if aggregation in {"add", "aggregate", "rollup", "total"}:
                aggregation = "sum"
            elif aggregation in {"minus", "difference"}:
                aggregation = "subtract"
            elif aggregation not in {"direct", "sum", "subtract", "none"}:
                aggregation = "none" if row_kind == "section" or not row.source_node_keys else "direct"

            if row_kind == "section" and not row.source_node_keys:
                aggregation = "none"
            if aggregation == "none" and row.source_node_keys:
                aggregation = "direct"
            if visibility not in {"default", "detail"}:
                visibility = "detail" if row.depth >= 2 else "default"

            row.row_kind = row_kind
            row.aggregation = aggregation
            row.visibility = visibility

            if row.depth >= 2:
                row.visibility = "detail"
            elif row.row_kind == "section" or row.aggregation == "none":
                row.visibility = "default"
            if row.parent_row_key:
                children[row.parent_row_key].append(row)

        for parent_key, child_rows in children.items():
            parent = by_key.get(parent_key)
            if parent is None or parent.row_kind != "section":
                continue
            source_bound = [
                child
                for child in child_rows
                if child.depth <= 1
                and child.aggregation != "none"
                and bool(child.source_node_keys)
            ]
            if len(source_bound) == 1:
                source_bound[0].visibility = "default"

        for index, row in enumerate(statement.rows):
            if row.row_kind != "section" or row.source_node_keys:
                continue
            following: list[RawFilingDisplayRowSpec] = []
            for candidate in statement.rows[index + 1:]:
                if candidate.depth == 0:
                    break
                if (
                    candidate.depth <= 1
                    and candidate.aggregation != "none"
                    and candidate.source_node_keys
                ):
                    following.append(candidate)
            if len(following) == 1:
                following[0].visibility = "default"
    return spec


def _execute_statement_spec(
    stmt: SourceStatement,
    spec: RawFilingStatementDisplaySpec,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_by_key = {row.node_key: row for row in stmt.rows}
    diagnostics: list[dict[str, Any]] = []
    rows_out: list[dict[str, Any]] = []
    source_use = Counter(
        source
        for row in spec.rows
        for source in row.source_node_keys
    )
    for source, count in source_use.items():
        if count > 1:
            diagnostics.append({
                "severity": "warning",
                "key": "duplicate_source_usage",
                "message": f"Source row {source} is used by {count} display rows.",
                "row_key": None,
                "details": {"source_node_key": source, "count": count},
            })

    for order, row in enumerate(spec.rows, start=1):
        sources = [source_by_key[key] for key in row.source_node_keys if key in source_by_key]
        values: dict[str, Decimal | None] = {}
        units: dict[str, str | None] = {}
        for col in stmt.columns:
            key = col.column_key
            if row.aggregation == "none" or not sources:
                values[key] = None
                units[key] = None
                continue
            source_values = [src.values.get(key) for src in sources]
            non_null = [value for value in source_values if value is not None]
            if not non_null:
                values[key] = None
            elif row.aggregation == "subtract":
                first = source_values[0]
                if first is None:
                    values[key] = None
                else:
                    values[key] = first - sum((value or Decimal("0")) for value in source_values[1:])
            else:
                values[key] = sum(non_null)
            source_units = {src.units.get(key) for src in sources if src.units.get(key)}
            units[key] = next(iter(source_units)) if len(source_units) == 1 else None
        rows_out.append({
            "spec": row,
            "display_order": order,
            "sources": sources,
            "values": values,
            "units": units,
        })

    by_key = {row["spec"].row_key: row for row in rows_out}
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows_out:
        parent = row["spec"].parent_row_key
        if parent:
            children[parent].append(row)

    tolerance_abs = Decimal("1")
    tolerance_pct = Decimal("0.02")
    for parent_key, child_rows in children.items():
        parent = by_key.get(parent_key)
        if not parent:
            continue
        for col in stmt.columns:
            pval = parent["values"].get(col.column_key)
            child_vals = [child["values"].get(col.column_key) for child in child_rows]
            non_null = [value for value in child_vals if value is not None]
            if pval is None or not non_null:
                continue
            child_sum = sum(non_null)
            delta = pval - child_sum
            threshold = max(tolerance_abs, abs(pval) * tolerance_pct)
            if abs(delta) > threshold:
                diagnostics.append({
                    "severity": "warning",
                    "key": "additive_coverage_delta",
                    "message": f"Children do not fully explain {parent_key} for {col.label}.",
                    "row_key": parent_key,
                    "details": {
                        "column_key": col.column_key,
                        "parent_value": str(pval),
                        "child_sum": str(child_sum),
                        "delta": str(delta),
                    },
                })
    return rows_out, diagnostics


def _insert_run(
    cur,
    *,
    source: SourceStatement,
    source_statement_ids: list[int],
    model: str,
    input_fingerprint: str,
    force: bool,
) -> tuple[int, bool]:
    cur.execute(
        """
        SELECT llm_display_run_id
        FROM fact_llm_raw_filing_display_run
        WHERE jurisdiction = %s
          AND entity_id = %s
          AND filing_id = %s
          AND prompt_version = %s
          AND schema_version = %s
          AND input_fingerprint = %s
          AND status = 'succeeded'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            source.jurisdiction,
            source.entity_id,
            source.filing_id,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            input_fingerprint,
        ),
    )
    cached = cur.fetchone()
    if cached and not force:
        return int(cached[0]), True

    cur.execute(
        """
        INSERT INTO fact_llm_raw_filing_display_run
            (jurisdiction, entity_id, ticker, filing_id, filing_form, filed_date,
             fiscal_year, fiscal_period, period_end, prompt_version, schema_version,
             model_name, input_fingerprint, source_statement_display_ids, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running')
        ON CONFLICT (jurisdiction, entity_id, filing_id, prompt_version, schema_version, input_fingerprint)
        DO UPDATE SET
            ticker = EXCLUDED.ticker,
            filing_form = EXCLUDED.filing_form,
            filed_date = EXCLUDED.filed_date,
            fiscal_year = EXCLUDED.fiscal_year,
            fiscal_period = EXCLUDED.fiscal_period,
            period_end = EXCLUDED.period_end,
            model_name = EXCLUDED.model_name,
            source_statement_display_ids = EXCLUDED.source_statement_display_ids,
            status = 'running',
            error_message = NULL,
            diagnostics = '{}'::jsonb,
            raw_llm_response = NULL,
            updated_at = now(),
            completed_at = NULL
        RETURNING llm_display_run_id
        """,
        (
            source.jurisdiction,
            source.entity_id,
            source.ticker,
            source.filing_id,
            source.filing_form,
            source.filed_date,
            source.fiscal_year,
            source.fiscal_period,
            source.period_end,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            model,
            input_fingerprint,
            source_statement_ids,
        ),
    )
    return int(cur.fetchone()[0]), False


def _mark_run(
    cur,
    run_id: int,
    *,
    status: str,
    diagnostics: dict[str, Any] | None = None,
    raw_response: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    cur.execute(
        """
        UPDATE fact_llm_raw_filing_display_run
        SET status = %s,
            diagnostics = %s,
            raw_llm_response = %s,
            error_message = %s,
            updated_at = now(),
            completed_at = now()
        WHERE llm_display_run_id = %s
        """,
        (
            status,
            _pg_json(diagnostics or {}),
            _pg_json(raw_response) if raw_response is not None else None,
            error_message,
            run_id,
        ),
    )


def _persist_statement(
    cur,
    *,
    run_id: int,
    stmt: SourceStatement,
    spec: RawFilingStatementDisplaySpec,
    rows_out: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    model: str,
    input_fingerprint: str,
) -> tuple[int, int, int]:
    cur.execute(
        """
        DELETE FROM fact_llm_raw_filing_statement_display
        WHERE jurisdiction = %s
          AND entity_id = %s
          AND filing_id = %s
          AND api_statement = %s
          AND prompt_version = %s
          AND schema_version = %s
          AND input_fingerprint = %s
        """,
        (
            stmt.jurisdiction,
            stmt.entity_id,
            stmt.filing_id,
            stmt.api_statement,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            input_fingerprint,
        ),
    )
    cur.execute(
        """
        INSERT INTO fact_llm_raw_filing_statement_display
            (llm_display_run_id, source_statement_display_id, jurisdiction, entity_id,
             ticker, filing_id, filing_form, filed_date, fiscal_year, fiscal_period,
             period_end, accounting_standard, api_statement, statement_type,
             statement_title, display_title, role_uri, prompt_version, schema_version,
             model_name, input_fingerprint, status, diagnostics)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'succeeded', %s)
        RETURNING llm_statement_display_id
        """,
        (
            run_id,
            stmt.statement_display_id,
            stmt.jurisdiction,
            stmt.entity_id,
            stmt.ticker,
            stmt.filing_id,
            stmt.filing_form,
            stmt.filed_date,
            stmt.fiscal_year,
            stmt.fiscal_period,
            stmt.period_end,
            stmt.accounting_standard,
            stmt.api_statement,
            stmt.statement_type,
            stmt.statement_title,
            spec.display_title,
            stmt.role_uri,
            PROMPT_VERSION,
            SCHEMA_VERSION,
            model,
            input_fingerprint,
            _pg_json({"items": diagnostics}),
        ),
    )
    llm_statement_id = int(cur.fetchone()[0])
    for col in stmt.columns:
        cur.execute(
            """
            INSERT INTO fact_llm_raw_filing_display_column
                (llm_statement_display_id, column_key, label, column_kind,
                 period_start, period_end, fiscal_year, fiscal_period, column_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                llm_statement_id,
                col.column_key,
                col.label,
                col.column_kind,
                col.period_start,
                col.period_end,
                col.fiscal_year,
                col.fiscal_period,
                col.column_order,
            ),
        )

    value_count = 0
    source_count = 0
    for row_out in rows_out:
        row: RawFilingDisplayRowSpec = row_out["spec"]
        cur.execute(
            """
            INSERT INTO fact_llm_raw_filing_display_row
                (llm_statement_display_id, row_key, parent_row_key, display_label,
                 row_kind, aggregation, visibility, display_depth, display_order,
                 confidence, rationale, raw_spec)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING row_id
            """,
            (
                llm_statement_id,
                row.row_key,
                row.parent_row_key,
                row.display_label,
                row.row_kind,
                row.aggregation,
                row.visibility,
                row.depth,
                int(row_out["display_order"]),
                Decimal(str(row.confidence)),
                row.rationale,
                _pg_json(_dump_model(row)),
            ),
        )
        row_id = int(cur.fetchone()[0])
        for source_order, source in enumerate(row_out["sources"]):
            weight = Decimal("-1") if row.aggregation == "subtract" and source_order > 0 else Decimal("1")
            cur.execute(
                """
                INSERT INTO fact_llm_raw_filing_display_row_source
                    (row_id, source_node_key, source_concept_id, aggregation_weight, source_order)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (row_id, source.node_key, source.source_concept_id, weight, source_order),
            )
            source_count += 1
        for col in stmt.columns:
            value = row_out["values"].get(col.column_key)
            unit = row_out["units"].get(col.column_key)
            provenance = "section" if row.aggregation == "none" else "direct" if row.aggregation == "direct" else "aggregated"
            cur.execute(
                """
                INSERT INTO fact_llm_raw_filing_display_value
                    (row_id, column_key, value, unit, provenance)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (row_id, col.column_key, value, unit, provenance),
            )
            value_count += 1
    for diag in diagnostics:
        cur.execute(
            """
            INSERT INTO fact_llm_raw_filing_display_diagnostic
                (llm_statement_display_id, row_key, severity, diagnostic_key, message, details)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                llm_statement_id,
                diag.get("row_key"),
                diag.get("severity", "warning"),
                diag.get("key", "diagnostic"),
                diag.get("message", ""),
                _pg_json(diag.get("details") or {}),
            ),
        )
    return 1, source_count, value_count


def build_llm_raw_filing_display(
    jurisdiction: Literal["US", "JP"],
    *,
    entity_id: str | None = None,
    ticker: str | None = None,
    filing_id: str | None = None,
    fiscal_period: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    statements: Iterable[str] | None = None,
    force: bool = False,
    model: str = DEFAULT_MODEL,
    heuristic_only: bool = False,
    llm: Any | None = None,
) -> dict[str, Any]:
    """Generate and persist an LLM display spec over DB-stored filing rows."""
    if not entity_id and not ticker:
        raise ValueError("Either entity_id or ticker is required.")

    with connect() as conn:
        with conn.cursor() as cur:
            source_rows = _resolve_source_statements(
                cur,
                jurisdiction=jurisdiction,
                entity_id=entity_id,
                ticker=ticker,
                filing_id=filing_id,
                fiscal_period=fiscal_period,
                year_min=year_min,
                year_max=year_max,
                statements=statements,
            )
            if not source_rows:
                return {
                    "status": "missing_raw_filing_pack",
                    "statements": 0,
                    "rows": 0,
                    "values": 0,
                    "message": "No fact_filing_statement_display rows found for this scope.",
                }
            source_statements = [_load_source_statement(cur, row) for row in source_rows]
            packet = build_input_packet(source_statements)
            input_fingerprint = _fingerprint(packet)
            run_id, cached = _insert_run(
                cur,
                source=source_statements[0],
                source_statement_ids=[stmt.statement_display_id for stmt in source_statements],
                model="heuristic" if heuristic_only else model,
                input_fingerprint=input_fingerprint,
                force=force,
            )
            if cached:
                return {
                    "status": "cached",
                    "run_id": run_id,
                    "statements": len(source_statements),
                    "rows": 0,
                    "values": 0,
                    "input_fingerprint": input_fingerprint,
                }

            try:
                spec = _heuristic_spec(packet) if heuristic_only else _call_llm(packet, model=model, llm=llm)
                spec = _normalize_spec_visibility(spec)
                raw_spec = _dump_model(spec)
                errors = _validation_errors(spec, packet)
                if errors:
                    _mark_run(
                        cur,
                        run_id,
                        status="failed_validation",
                        diagnostics={"errors": errors},
                        raw_response=raw_spec,
                        error_message="LLM display spec failed validation.",
                    )
                    return {
                        "status": "failed_validation",
                        "run_id": run_id,
                        "statements": 0,
                        "rows": 0,
                        "values": 0,
                        "errors": errors,
                    }

                spec_by_statement = {stmt.api_statement: stmt for stmt in spec.statements}
                statement_count = 0
                source_count = 0
                value_count = 0
                all_diagnostics: dict[str, Any] = {"items": []}
                for source_stmt in source_statements:
                    stmt_spec = spec_by_statement.get(source_stmt.api_statement)
                    if stmt_spec is None:
                        all_diagnostics["items"].append({
                            "severity": "warning",
                            "key": "missing_statement_spec",
                            "message": f"No LLM spec returned for {source_stmt.api_statement}.",
                        })
                        continue
                    rows_out, diagnostics = _execute_statement_spec(source_stmt, stmt_spec)
                    counts = _persist_statement(
                        cur,
                        run_id=run_id,
                        stmt=source_stmt,
                        spec=stmt_spec,
                        rows_out=rows_out,
                        diagnostics=diagnostics,
                        model="heuristic" if heuristic_only else model,
                        input_fingerprint=input_fingerprint,
                    )
                    statement_count += counts[0]
                    source_count += counts[1]
                    value_count += counts[2]
                    all_diagnostics["items"].extend(diagnostics)
                _mark_run(
                    cur,
                    run_id,
                    status="succeeded",
                    diagnostics=all_diagnostics,
                    raw_response=raw_spec,
                )
                return {
                    "status": "succeeded",
                    "run_id": run_id,
                    "statements": statement_count,
                    "rows": sum(len(stmt.rows) for stmt in spec.statements),
                    "sources": source_count,
                    "values": value_count,
                    "diagnostics": len(all_diagnostics["items"]),
                    "input_fingerprint": input_fingerprint,
                }
            except Exception as exc:
                _mark_run(
                    cur,
                    run_id,
                    status="failed",
                    diagnostics={},
                    raw_response=None,
                    error_message=str(exc)[:1000],
                )
                raise


def build_llm_raw_filing_display_for_available_universe(
    jurisdiction: Literal["US", "JP"],
    *,
    limit: int | None = None,
    force: bool = False,
    model: str = DEFAULT_MODEL,
    heuristic_only: bool = False,
) -> dict[str, Any]:
    """Batch helper for filings already present in fact_filing_statement_display."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entity_id, ticker, filing_id, fiscal_year, fiscal_period,
                       COUNT(DISTINCT api_statement) AS statements
                FROM fact_filing_statement_display
                WHERE jurisdiction = %s
                GROUP BY entity_id, ticker, filing_id, fiscal_year, fiscal_period
                HAVING COUNT(DISTINCT api_statement) >= 3
                ORDER BY fiscal_year DESC NULLS LAST, filing_id DESC
                LIMIT %s
                """,
                (jurisdiction, limit),
            )
            rows = cur.fetchall()
    completed = 0
    failed = 0
    missing = 0
    for row in rows:
        try:
            result = build_llm_raw_filing_display(
                jurisdiction,
                entity_id=row[0],
                ticker=row[1],
                filing_id=row[2],
                force=force,
                model=model,
                heuristic_only=heuristic_only,
            )
            if result.get("status") in {"succeeded", "cached"}:
                completed += 1
            elif result.get("status") == "missing_raw_filing_pack":
                missing += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"candidates": len(rows), "completed": completed, "failed": failed, "missing": missing}
