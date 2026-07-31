"""Build formula recon tables from computed metrics."""
from __future__ import annotations

import re

from xbrl_sec.sec.local_deps import add_project_deps

add_project_deps()
from psycopg2.extras import Json

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics.compute import (
    _ANNUAL_FP,
    _eval_formula,
    _f,
    _load_entity_tickers,
    _load_prices,
    _load_std_facts,
    _namespace,
    _price_at,
    _price_metrics,
    _shares_for_market_cap,
    _topo_order,
)
from xbrl_sec.sec.metrics.formulas import _L1_FORMULAS
from xbrl_sec.sec.state.store import finish_run, start_run, update_run_progress


_NON_VARS = {
    "_div", "pct_change", "cagr", "_tax_rate", "_abs_capex", "_f",
    "abs", "max", "min", "None", "True", "False", "math",
    "or", "if", "else", "not", "and", "is",
}


def _formula_tokens(formula: str | None) -> list[str]:
    if not formula:
        return []
    out: list[str] = []
    for token in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", formula):
        if token in _NON_VARS or token.startswith("_"):
            continue
        out.append(token)
    return sorted(set(out))


def _base_line_item(token: str) -> tuple[str, str]:
    for suffix in ("_prev", "_3y", "_5y"):
        if token.endswith(suffix):
            return token[: -len(suffix)], suffix[1:]
    return token, "current"


def _fmt(value):
    if value is None:
        return "None"
    try:
        f = float(value)
        if f == int(f) and abs(f) < 1e15:
            return str(int(f))
        return f"{f:g}"
    except Exception:
        return str(value)


def _substitute(formula: str, namespace: dict) -> str:
    def repl(match):
        name = match.group(0)
        if name in _NON_VARS:
            return name
        if name in namespace:
            return f"{_fmt(namespace[name])} [{name}]"
        return name

    return re.sub(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", repl, formula or "")


def _metric_rows(metric_table: str, entity_col: str, entities: list[str] | None = None) -> list[tuple]:
    entity_filter = ""
    params: list = []
    if entities:
        entity_filter = f"AND {entity_col} = ANY(%s)"
        params.append(entities)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ticker, {entity_col}, fiscal_year, fiscal_period, period_end,
                   metric_id, formula, value, currency, metric_type, category,
                   importance, unit_type, fallback_applied
            FROM {metric_table}
            WHERE formula IS NOT NULL
              {entity_filter}
            ORDER BY {entity_col}, fiscal_year, fiscal_period, metric_id
            """,
            params,
        )
        return cur.fetchall()


def _load_std_trace(std_table: str, entity_col: str, jurisdiction: str, entities: list[str]) -> dict:
    if not entities:
        return {}
    context_cols = ""
    if jurisdiction == "JP":
        context_cols = ", context_id, dimension_signature"
    else:
        context_cols = ", NULL::text AS context_id, NULL::text AS dimension_signature"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {entity_col}, fiscal_year, fiscal_period, line_item_id, value,
                   source_concept_id, filing_id, filing_form, filed_date, concept_path,
                   std_concept_path, metric_type, currency, mapping_id {context_cols}
            FROM {std_table}
            WHERE {entity_col} = ANY(%s)
              AND value IS NOT NULL
            """,
            (entities,),
        )
        out = {}
        for row in cur.fetchall():
            (
                entity_id, fy, fp, line_item, value, source_concept, filing_id,
                filing_form, filed_date, concept_path, std_concept_path,
                metric_type, currency, mapping_id, context_id, dimension_signature,
            ) = row
            out.setdefault(entity_id, {}).setdefault((int(fy), fp), {})[line_item] = {
                "line_item_id": line_item,
                "value": float(value) if value is not None else None,
                "source_concept_id": source_concept,
                "filing_id": filing_id,
                "filing_form": filing_form,
                "filed_date": filed_date.isoformat() if filed_date else None,
                "concept_path": concept_path,
                "std_concept_path": std_concept_path,
                "metric_type": metric_type,
                "currency": currency,
                "mapping_id": mapping_id,
                "context_id": context_id,
                "dimension_signature": dimension_signature,
            }
        return out


def _metric_namespace(rows: list[tuple]) -> dict:
    out = {}
    for row in rows:
        _ticker, entity_id, fy, fp, _pe, metric_id, _formula, value, _currency = row[:9]
        out.setdefault((entity_id, fy, fp), {})[metric_id] = value
    return out


def _compute_l1_timeseries(entity_ts: dict, entity_meta: dict, price_data: dict, order: list[str], jurisdiction: str) -> dict:
    keys = sorted(entity_ts.keys())
    fy_annual = {fy: key for fy, key in ((key[0], key) for key in keys if key[1] in _ANNUAL_FP)}
    close_map = price_data.get("close", {})
    l1_ts: dict[tuple, dict] = {}
    for key in keys:
        fy, fp = key
        l0 = entity_ts[key]
        pe, _currency, _jur, *_rest = entity_meta.get(key, (None, None, jurisdiction, None))
        px = _price_at(close_map, pe)
        shares = _shares_for_market_cap(l0)
        market_cap = px * shares if px and shares and shares > 0 else None
        prev_key = (fy - 1, fp) if (fy - 1, fp) in entity_ts else fy_annual.get(fy - 1)
        ns = dict(l0)
        for item, val in entity_ts.get(prev_key, {}).items():
            ns[item + "_prev"] = val
        for item, val in l1_ts.get(prev_key, {}).items():
            ns[item + "_prev"] = val
        ns["market_cap"] = market_cap
        ns["price"] = px
        l1 = {}
        for node in order:
            if node not in _L1_FORMULAS or node in l0:
                continue
            val = _eval_formula(_L1_FORMULAS[node], {**ns, **l1})
            if val is not None:
                l1[node] = val
        l1_ts[key] = l1
    return l1_ts


def _clear(table: str, entity_col: str, entity_ids: list[str] | None) -> None:
    with connect() as conn, conn.cursor() as cur:
        if entity_ids:
            cur.execute(f"DELETE FROM {table} WHERE {entity_col} = ANY(%s)", (entity_ids,))
        else:
            cur.execute(f"TRUNCATE {table}")


def _write(recon_table: str, entity_col: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {recon_table}
            (ticker, {entity_col}, fiscal_year, fiscal_period, period_end,
             metric_id, formula, formula_with_values, value, currency,
             metric_type, category, importance, unit_type, fallback_applied,
             input_values, source_line_items, source_concept_ids, source_filing_ids,
             raw_trace, trace_quality)
        VALUES %s
        ON CONFLICT (ticker, {entity_col}, fiscal_year, fiscal_period, metric_id)
        DO UPDATE SET
            period_end = EXCLUDED.period_end,
            formula = EXCLUDED.formula,
            formula_with_values = EXCLUDED.formula_with_values,
            value = EXCLUDED.value,
            currency = EXCLUDED.currency,
            metric_type = EXCLUDED.metric_type,
            category = EXCLUDED.category,
            importance = EXCLUDED.importance,
            unit_type = EXCLUDED.unit_type,
            fallback_applied = EXCLUDED.fallback_applied,
            input_values = EXCLUDED.input_values,
            source_line_items = EXCLUDED.source_line_items,
            source_concept_ids = EXCLUDED.source_concept_ids,
            source_filing_ids = EXCLUDED.source_filing_ids,
            raw_trace = EXCLUDED.raw_trace,
            trace_quality = EXCLUDED.trace_quality,
            computed_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows, page_size=5000)


def _trace_for_formula(
    formula: str | None,
    namespace: dict,
    std_trace: dict,
    current_key: tuple,
    prev_key: tuple | None,
    key_3y: tuple | None,
    key_5y: tuple | None,
) -> tuple[dict, list[dict], str]:
    input_values: dict[str, float | None] = {}
    trace_rows: list[dict] = []
    seen_trace: set[tuple] = set()
    missing_inputs = 0
    unresolved_inputs = 0
    for token in _formula_tokens(formula):
        base, period_role = _base_line_item(token)
        value = namespace.get(token)
        input_values[token] = float(value) if value is not None else None
        if value is None:
            missing_inputs += 1
        period_key = current_key
        if period_role == "prev":
            period_key = prev_key
        elif period_role == "3y":
            period_key = key_3y
        elif period_role == "5y":
            period_key = key_5y
        source = std_trace.get(period_key, {}).get(base) if period_key else None
        if source is None:
            if value is not None:
                unresolved_inputs += 1
            continue
        trace_key = (period_role, source.get("line_item_id"), source.get("filing_id"), source.get("source_concept_id"))
        if trace_key in seen_trace:
            continue
        seen_trace.add(trace_key)
        item = dict(source)
        item["formula_token"] = token
        item["period_role"] = period_role
        if period_key is not None:
            item["source_fiscal_year"] = period_key[0]
            item["source_fiscal_period"] = period_key[1]
        trace_rows.append(item)
    if not trace_rows and input_values:
        quality = "computed_only"
    elif missing_inputs:
        quality = "partial_missing_inputs"
    elif unresolved_inputs:
        quality = "partial_computed_inputs"
    else:
        quality = "full_source_trace"
    return input_values, trace_rows, quality


# Entity-chunked recon builds. Smaller than standardize's chunk because recon
# loads heavier per-entity payload: metrics + prices + std_facts + std_trace +
# computed l1 timeseries. Per chunk peak ~2-4 GB resident; tune lower if you
# still OOM, higher to reduce DB round-trip overhead.
_RECON_CHUNK_SIZE = 100


def _recon_is_fresh(metric_table: str, recon_table: str) -> bool:
    """True iff `recon_table.computed_at` covers `metric_table.computed_at`.

    Same NULL semantics as the std and metrics gates: empty metrics → nothing
    to reconcile, fresh. Empty recon with non-empty metrics → not fresh.
    """
    # Both columns are TIMESTAMP (naive); SQL comparison is fine but we keep
    # the same shape as _metrics_is_fresh for consistency.
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH m AS (SELECT max(computed_at) AS t FROM {metric_table}),
                 r AS (SELECT max(computed_at) AS t FROM {recon_table})
            SELECT
                (SELECT t FROM m) IS NULL                 AS metric_empty,
                (SELECT t FROM r) IS NULL                 AS recon_empty,
                COALESCE((SELECT t FROM r) >= (SELECT t FROM m), FALSE) AS fresh
            """
        )
        metric_empty, recon_empty, fresh = cur.fetchone()
    if metric_empty:
        return True
    if recon_empty:
        return False
    return bool(fresh)


def _entities_with_metrics(metric_table: str, entity_col: str, entity_ids: list[str] | None) -> list[str]:
    """Distinct entity IDs in `metric_table` that have at least one formula-bearing row.

    Used by build_recon to drive per-entity chunking without ever materializing
    the full metric row set in memory just to discover its entity set.
    """
    entity_filter = ""
    params: list = []
    if entity_ids:
        entity_filter = f"AND {entity_col} = ANY(%s)"
        params.append(entity_ids)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT {entity_col}
              FROM {metric_table}
             WHERE formula IS NOT NULL
              {entity_filter}
             ORDER BY {entity_col}
            """,
            params,
        )
        return [row[0] for row in cur.fetchall()]


def build_recon(
    jurisdiction: str,
    entity_ids: list[str] | None = None,
    full: bool = False,
    chunk_size: int = _RECON_CHUNK_SIZE,
) -> int:
    """Build the formula recon table for `jurisdiction`.

    Entity-chunked to bound memory. Each chunk loads its own metrics, prices,
    std facts, std traces, builds per-entity l1 timeseries, computes recon
    rows, upserts, and drops the chunk's allocations before the next chunk.

    Semantics preserved from the previous monolithic version:

    - `_compute_l1_timeseries` and the metric-loop traces are all entity-keyed
      lookups (`ts.get(entity_id)`, `l1_by_entity.get(entity_id)`,
      `std_trace.get(entity_id)`, `metric_ns.get((entity_id, fy, fp))`) — no
      cross-entity dependencies, so chunking by entity changes no output row.
    - `full=True` with no entity_ids → TRUNCATE recon once up front, then
      upsert each chunk (rely on ON CONFLICT for re-runs of same chunk).
    - `entity_ids` set → DELETE those entities once up front, then upsert.
    - Neither flag set → no clear; rely on ON CONFLICT DO UPDATE.
    """
    cfg = {
        "US": ("fact_fundamentals_std_us", "fact_prices_us", "fact_metrics_us", "fact_metrics_recon_us", "cik", "CIK"),
        "JP": ("fact_fundamentals_std_jp", "fact_prices_jp", "fact_metrics_jp", "fact_metrics_recon_jp", "edinet_code", "EDINET_CODE"),
    }[jurisdiction]
    std_table, price_table, metric_table, recon_table, entity_col, entity_id_type = cfg
    ctx = start_run(jurisdiction, "recon", "full_refresh" if full else "incremental")
    try:
        if not full and not entity_ids and _recon_is_fresh(metric_table, recon_table):
            print(f"{jurisdiction} recon: skipped ({recon_table} already covers {metric_table})", flush=True)
            finish_run(ctx, "succeeded", rows_in=0, rows_out=0)
            return 0
        # One-time setup, shared across chunks.
        order = _topo_order()
        tickers_by_entity = _load_entity_tickers(entity_id_type)

        # Single up-front delete; per-chunk upserts then rely on ON CONFLICT.
        if full and not entity_ids:
            _clear(recon_table, entity_col, None)
        elif entity_ids:
            _clear(recon_table, entity_col, list(entity_ids))

        # Determine the entity universe to chunk over.
        if entity_ids:
            scope = list(entity_ids)
        else:
            scope = _entities_with_metrics(metric_table, entity_col, None)

        total_metrics = 0
        total_written = 0

        for chunk_start in range(0, len(scope), chunk_size):
            chunk = scope[chunk_start : chunk_start + chunk_size]

            # Load per-chunk inputs. Metrics is the driver; if a chunk has no
            # formula rows we skip without touching the heavier loaders.
            metrics = _metric_rows(metric_table, entity_col, chunk)
            if not metrics:
                continue
            chunk_entities = sorted({row[1] for row in metrics})
            chunk_tickers = sorted({
                ticker
                for entity in chunk_entities
                for ticker in tickers_by_entity.get(entity, [])
            })
            prices = _load_prices(price_table, chunk_tickers)
            ts, meta = _load_std_facts(std_table, entity_col, chunk_entities)
            std_trace = _load_std_trace(std_table, entity_col, jurisdiction, chunk_entities)
            metric_ns = _metric_namespace(metrics)

            # Build per-entity l1 timeseries for this chunk only.
            l1_by_entity: dict = {}
            for entity_id in chunk_entities:
                tickers = tickers_by_entity.get(entity_id, [])
                primary = tickers[0] if tickers else None
                if entity_id in ts:
                    l1_by_entity[entity_id] = _compute_l1_timeseries(
                        ts[entity_id],
                        meta[entity_id],
                        prices.get(primary, {"close": {}, "return": {}}),
                        order,
                        jurisdiction,
                    )

            # Build recon rows for this chunk.
            rows: list[tuple] = []
            for (
                ticker, entity_id, fy, fp, pe, metric_id, formula, value, currency,
                metric_type, category, importance, unit_type, fallback_applied,
            ) in metrics:
                key = (fy, fp)
                entity_ts = ts.get(entity_id, {})
                entity_meta = meta.get(entity_id, {})
                l1_ts = l1_by_entity.get(entity_id, {})
                annual = {year: period_key for year, period_key in ((k[0], k) for k in entity_ts if k[1] in _ANNUAL_FP)}
                prev_key = (fy - 1, fp) if (fy - 1, fp) in entity_ts else annual.get(fy - 1)
                key_3y = annual.get(fy - 3)
                key_5y = annual.get(fy - 5)
                price_data = prices.get(ticker, {"close": {}, "return": {}})
                px = _price_at(price_data.get("close", {}), pe)
                shares = _shares_for_market_cap(entity_ts.get(key, {}))
                market_cap = px * shares if px and shares and shares > 0 else None
                ns = _namespace(
                    entity_ts.get(key, {}), l1_ts.get(key, {}),
                    entity_ts.get(prev_key, {}) if prev_key else {}, l1_ts.get(prev_key, {}) if prev_key else {},
                    entity_ts.get(key_3y, {}) if key_3y else {}, l1_ts.get(key_3y, {}) if key_3y else {},
                    entity_ts.get(key_5y, {}) if key_5y else {}, l1_ts.get(key_5y, {}) if key_5y else {},
                    market_cap, px, _price_metrics(price_data.get("return", {}), pe),
                )
                ns.update(metric_ns.get((entity_id, fy, fp), {}))
                input_values, trace_rows, trace_quality = _trace_for_formula(
                    formula,
                    ns,
                    std_trace.get(entity_id, {}),
                    key,
                    prev_key,
                    key_3y,
                    key_5y,
                )
                source_line_items = sorted({item["line_item_id"] for item in trace_rows if item.get("line_item_id")})
                source_concept_ids = sorted({item["source_concept_id"] for item in trace_rows if item.get("source_concept_id")})
                source_filing_ids = sorted({item["filing_id"] for item in trace_rows if item.get("filing_id")})
                rows.append((
                    ticker, entity_id, fy, fp, pe, metric_id, formula, _substitute(formula, ns),
                    value, currency, metric_type, category, importance, unit_type, fallback_applied,
                    Json(input_values), source_line_items, source_concept_ids, source_filing_ids,
                    Json(trace_rows), trace_quality,
                ))

            chunk_metric_count = len(metrics)
            chunk_written = _write(recon_table, entity_col, rows)
            total_metrics += chunk_metric_count
            total_written += chunk_written
            update_run_progress(ctx, rows_in=total_metrics, rows_out=total_written)

            # Release this chunk's heavy allocations before moving on.
            del metrics, prices, ts, meta, std_trace, metric_ns, l1_by_entity, rows

        finish_run(ctx, "succeeded", rows_in=total_metrics, rows_out=total_written)
        return total_written
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
