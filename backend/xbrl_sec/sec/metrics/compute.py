"""Compute fact_metrics_us/jp from standardized fundamentals."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta
import math
import re
from statistics import stdev
from typing import Any

from xbrl_sec.sec.db.bulk import execute_values
from xbrl_sec.sec.db.connection import connect
from xbrl_sec.sec.metrics.formulas import (
    _L1_FORMULAS,
    _LINE_ITEM_RENAMES,
    _METRIC_FORMULAS,
    _METRIC_PRIMARY_REQUIRED,
    _METRIC_RENAMES,
)
from xbrl_sec.sec.state.store import finish_run, start_run


_NON_VARS = {
    "_div", "pct_change", "cagr", "_tax_rate", "_abs_capex", "_f",
    "abs", "max", "min", "None", "True", "False", "math",
    "price_mom_12m_1m", "price_mom_6m",
    "market_cap", "price",
}
_PYTHON_KEYWORDS = {
    "if", "else", "and", "or", "not", "in", "is", "None", "True", "False",
    "return", "for", "while", "def", "class", "import", "from", "as",
    "try", "except", "raise", "with", "yield", "lambda", "pass", "break",
    "continue", "del", "assert", "global", "nonlocal", "elif",
}
_ANNUAL_FP = {"FY", "Annual"}
_DILUTED_SHARES_ID = _LINE_ITEM_RENAMES.get("diluted_shares", "diluted_shares")
_MARKET_CAP_ID = _LINE_ITEM_RENAMES.get("market_cap", "market_cap")
_PRICE_ID = _LINE_ITEM_RENAMES.get("price", "price")
_PER_SHARE_LINE_ITEMS = {
    "earnings_per_share_basic",
    "earnings_per_share_diluted",
    "dividends_per_share",
    "earnings_per_share_consensus_forward_1_year",
    "earnings_per_share_consensus_forward_2_year",
}
_SHARE_COUNT_LINE_ITEMS = {
    "shares_outstanding",
    "shares_outstanding_basic",
    "shares_outstanding_diluted",
}


def _div(a, b):
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
        if b == 0 or math.isnan(a) or math.isnan(b):
            return None
        out = a / b
        return None if math.isinf(out) else out
    except (TypeError, ValueError):
        return None


def _pct_change(new, old):
    if new is None or old is None:
        return None
    try:
        new, old = float(new), float(old)
        if old == 0:
            return None
        out = (new - old) / abs(old)
        return None if math.isnan(out) or math.isinf(out) else out
    except (TypeError, ValueError):
        return None


def _cagr(v_end, v_start, years):
    if v_end is None or v_start is None:
        return None
    try:
        v_end, v_start, years = float(v_end), float(v_start), float(years)
        if v_end <= 0 or v_start <= 0 or years <= 0:
            return None
        out = (v_end / v_start) ** (1.0 / years) - 1
        return None if math.isnan(out) or math.isinf(out) else out
    except (TypeError, ValueError):
        return None


def _tax_rate(ctr):
    if ctr is None:
        return 0.25
    try:
        rate = float(ctr)
        return rate if 0 <= rate <= 1 else 0.25
    except (TypeError, ValueError):
        return 0.25


def _abs_capex(capex):
    return None if capex is None else abs(float(capex))


def _f(value):
    if value is None:
        return None
    try:
        out = float(value)
        return None if math.isnan(out) or math.isinf(out) else out
    except (TypeError, ValueError):
        return None


def _get_alias(values: dict[str, Any], legacy_id: str) -> Any:
    return values.get(_LINE_ITEM_RENAMES.get(legacy_id, legacy_id), values.get(legacy_id))


def _with_legacy_aliases(values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    for old, new in _LINE_ITEM_RENAMES.items():
        if old not in out and new in out:
            out[old] = out[new]
        for suffix in ("_prev", "_3y", "_5y"):
            old_key = old + suffix
            new_key = new + suffix
            if old_key not in out and new_key in out:
                out[old_key] = out[new_key]
    for old, new in _METRIC_RENAMES.items():
        if old not in out and new in out:
            out[old] = out[new]
        for suffix in ("_prev", "_3y", "_5y"):
            old_key = old + suffix
            new_key = new + suffix
            if old_key not in out and new_key in out:
                out[old_key] = out[new_key]
    return out


class _FormulaNamespace(dict):
    def __missing__(self, key):
        return None


_SAFE_EVAL_BASE = {
    "__builtins__": {},
    "_div": _div,
    "pct_change": _pct_change,
    "cagr": _cagr,
    "_tax_rate": _tax_rate,
    "_abs_capex": _abs_capex,
    "_f": _f,
    "abs": abs,
    "max": max,
    "min": min,
    "None": None,
    "True": True,
    "False": False,
}


def _eval_formula(formula: str, namespace: dict[str, Any]) -> float | None:
    try:
        ns = _FormulaNamespace(_SAFE_EVAL_BASE)
        ns.update(namespace)
        out = eval(formula, {"__builtins__": {}}, ns)  # noqa: S307
        if out is None:
            return None
        out = float(out)
        return None if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return None


def _deps(formula: str) -> set[str]:
    deps: set[str] = set()
    for token in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", formula or ""):
        if token in _PYTHON_KEYWORDS:
            continue
        base = token
        for suffix in ("_prev", "_3y", "_5y"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base not in _NON_VARS and not base.startswith("_"):
            deps.add(base)
    return deps


def _topo_order() -> list[str]:
    formulas = {**_L1_FORMULAS, **_METRIC_FORMULAS}
    nodes = set(formulas)
    incoming: dict[str, set[str]] = {node: set() for node in nodes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for node, formula in formulas.items():
        for dep in _deps(formula) - {node}:
            if dep in nodes:
                incoming[node].add(dep)
                outgoing[dep].add(node)
    queue = deque(sorted([node for node, deps in incoming.items() if not deps]))
    out: list[str] = []
    while queue:
        node = queue.popleft()
        out.append(node)
        for child in sorted(outgoing[node]):
            incoming[child].discard(node)
            if not incoming[child]:
                queue.append(child)
    if len(out) != len(nodes):
        remaining = sorted(node for node in nodes if node not in set(out))
        raise RuntimeError(f"Metric formula dependency cycle detected: {remaining}")
    return out


def _compute_special(ns: dict, prev_ns: dict) -> tuple[dict[str, float], dict[str, bool]]:
    out: dict[str, float] = {}
    fallback: dict[str, bool] = {}

    def _g(key):
        return _f(ns.get(key))

    def _p(key):
        return _f(prev_ns.get(key) if prev_ns else None)

    ta = _g("total_assets")
    re_ = _g("retained_earnings")
    ebit = _g("ebit")
    rev = _g("revenue")
    tl = _g("total_liabilities")
    mc = _g("market_cap")
    ca = _g("total_current_assets")
    cl = _g("total_current_liab")
    if ta and ta > 0 and tl and tl != 0 and re_ is not None and ebit is not None and rev is not None and mc:
        wc = (ca or 0) - (cl or 0)
        z_score = (
            1.2 * (wc / ta)
            + 1.4 * (re_ / ta)
            + 3.3 * (ebit / ta)
            + 0.6 * (mc / tl)
            + 1.0 * (rev / ta)
        )
        if not math.isnan(z_score) and not math.isinf(z_score):
            out["altman_z"] = z_score

    ni = _g("net_income")
    cfo = _g("cfo")
    ltd = _g("long_term_debt") or 0.0
    gp = _g("gross_profit")
    shares = _g("diluted_shares")
    p_ta = _p("total_assets")
    p_ni = _p("net_income")
    p_ltd = _p("long_term_debt") or 0.0
    p_ca = _p("total_current_assets")
    p_cl = _p("total_current_liab")
    p_shares = _p("diluted_shares")
    p_gp = _p("gross_profit")
    p_rev = _p("revenue")

    if ta and ta > 0:
        score = 0
        valid = 0
        if ni is not None:
            score += 1 if ni / ta > 0 else 0
            valid += 1
        if cfo is not None:
            score += 1 if cfo > 0 else 0
            valid += 1
        if ni is not None and cfo is not None:
            score += 1 if cfo / ta > ni / ta else 0
            valid += 1
        if p_ta and p_ta > 0:
            score += 1 if ltd / ta < p_ltd / p_ta else 0
            valid += 1
        if ca and cl and cl > 0 and p_ca and p_cl and p_cl > 0:
            score += 1 if (ca / cl) > (p_ca / p_cl) else 0
            valid += 1
        if shares is not None and p_shares is not None:
            score += 1 if shares <= p_shares else 0
            valid += 1
        if gp is not None and rev and rev > 0 and p_gp is not None and p_rev and p_rev > 0:
            score += 1 if (gp / rev) > (p_gp / p_rev) else 0
            valid += 1
        if rev is not None and p_rev is not None and p_ta and p_ta > 0:
            score += 1 if (rev / ta) > (p_rev / p_ta) else 0
            valid += 1
        if ni is not None and p_ni is not None and p_ta and p_ta > 0:
            score += 1 if (ni / ta) > (p_ni / p_ta) else 0
            valid += 1
        if valid >= 5:
            out["piotroski_f"] = float(score)
            fallback["piotroski_f"] = valid < 9

    ar_gross = _g("accounts_receivable_gross")
    ppe_gross = _g("ppe_gross")
    acc_dep = _g("accumulated_depreciation")
    ppe_g = ppe_gross or (_f((_g("ppe_net") or 0) + (acc_dep or 0)) if _g("ppe_net") else None)
    ar_g = ar_gross or _g("accounts_receivable")
    sga = _g("sga")
    p_acc_dep_gross = _p("accumulated_depreciation")
    p_acc_dep = p_acc_dep_gross or _p("da_addback")
    p_ppe_g = _p("ppe_gross") or (_f((_p("ppe_net") or 0) + (p_acc_dep or 0)) if _p("ppe_net") else None)
    p_ar_gross = _p("accounts_receivable_gross")
    p_ar_g = p_ar_gross or _p("accounts_receivable")
    p_sga = _p("sga")

    if all(x is not None and x != 0 for x in [p_rev, p_ar_g, p_ta, p_ca, p_cl, rev, ta]):
        try:
            dsri = _div(ar_g / rev, p_ar_g / p_rev) if ar_g and p_ar_g else None
            gmi = _div(p_gp / p_rev, gp / rev) if p_gp and gp and rev else None
            ca_t = (ca or 0) + (_g("ppe_net") or 0)
            ca_t1 = (p_ca or 0) + (_p("ppe_net") or 0)
            aqi = _div(1.0 - ca_t / ta, 1.0 - ca_t1 / p_ta) if ta != 0 and p_ta != 0 else None
            sgi = _div(rev, p_rev) if p_rev else None
            cur_dep = acc_dep or _g("da_addback")
            depi = None
            if ppe_g and cur_dep and p_ppe_g and p_acc_dep:
                denom_cur = ppe_g + cur_dep
                denom_prev = p_ppe_g + p_acc_dep
                if denom_cur != 0 and denom_prev != 0:
                    depi = _div(p_acc_dep / denom_prev, cur_dep / denom_cur)
            sgai = _div(sga / rev, p_sga / p_rev) if sga and p_sga and rev and p_rev else None
            ltd_cur = _g("long_term_debt") or 0.0
            lvgi = _div((ltd_cur + (cl or 0)) / ta, (p_ltd + p_cl) / p_ta) if p_cl and ta != 0 and p_ta != 0 else None
            tata = _div((ni or 0) - (cfo or 0), ta) if ni is not None and cfo is not None else None
            if all(x is not None for x in [dsri, gmi, aqi, sgi, tata]):
                m_score = (
                    -4.84
                    + 0.920 * dsri
                    + 0.528 * gmi
                    + 0.404 * aqi
                    + 0.892 * sgi
                    + 0.115 * (depi or 0.0)
                    - 0.172 * (sgai or 0.0)
                    + 4.679 * tata
                    - 0.327 * (lvgi or 0.0)
                )
                if not math.isnan(m_score) and not math.isinf(m_score):
                    out["beneish_m"] = m_score
                    fallback["beneish_m"] = (
                        ar_gross is None
                        or ppe_gross is None
                        or acc_dep is None
                        or p_ar_gross is None
                        or p_acc_dep_gross is None
                    )
        except Exception:
            pass

    if ta and ta > 0 and tl is not None and ni is not None and cfo and ca and cl and cl > 0:
        try:
            oeneg = 1 if tl > ta else 0
            p_ni_o = _p("net_income")
            intwo = 1 if ni < 0 and p_ni_o is not None and p_ni_o < 0 else 0
            chin = (ni - p_ni_o) / (abs(ni) + abs(p_ni_o)) if p_ni_o is not None and (abs(ni) + abs(p_ni_o)) > 0 else 0.0
            wc = (ca or 0) - (cl or 0)
            o_score = (
                -1.32
                - 0.407 * math.log(max(ta / 1000.0, 1.0))
                + 6.03 * (tl / ta)
                - 1.43 * (wc / ta)
                + 0.076 * (cl / ca)
                - 1.72 * oeneg
                - 2.37 * (ni / ta)
                - 1.83 * ((cfo / tl) if tl != 0 else 0)
                + 0.285 * intwo
                - 0.521 * chin
            )
            if not math.isnan(o_score) and not math.isinf(o_score):
                out["ohlson_o_score"] = o_score
                fallback["ohlson_o_score"] = False
        except Exception:
            pass

    return out, fallback


def _load_metric_meta() -> dict[str, tuple]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT metric_id, category, importance, unit_type, metric_type, formula
            FROM ref_metric_definitions
            """
        )
        return {row[0]: row[1:] for row in cur.fetchall()}


def _load_entity_tickers(entity_id_type: str) -> dict[str, list[str]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, ticker
            FROM ref_entity_ticker
            WHERE entity_id_type=%s
              AND is_primary
            ORDER BY entity_id, is_primary DESC, ticker
            """,
            (entity_id_type,),
        )
        out: dict[str, list[str]] = defaultdict(list)
        for entity_id, ticker in cur.fetchall():
            out[entity_id].append(ticker)
        return out


def _load_std_facts(std_table: str, entity_col: str, entity_ids: list[str]) -> tuple[dict, dict]:
    if not entity_ids:
        return {}, {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {entity_col}, jurisdiction, fiscal_year, fiscal_period,
                   period_end, line_item_id, value, currency, filed_date
            FROM {std_table}
            WHERE {entity_col} = ANY(%s)
              AND value IS NOT NULL
            ORDER BY {entity_col}, fiscal_year, fiscal_period, filed_date DESC NULLS LAST
            """,
            (entity_ids,),
        )
        ts = defaultdict(lambda: defaultdict(dict))
        meta = defaultdict(dict)
        for entity_id, jur, fy, fp, pe, line_item, value, currency, filed_date in cur.fetchall():
            key = (int(fy), fp)
            ts[entity_id][key][line_item] = float(value)
            if key not in meta[entity_id]:
                meta[entity_id][key] = (pe, currency, jur, filed_date)
            elif filed_date and (meta[entity_id][key][3] is None or filed_date > meta[entity_id][key][3]):
                meta[entity_id][key] = (pe, currency, jur, filed_date)
        return ts, meta


def _entities(std_table: str, entity_col: str, requested: list[str] | None, full: bool) -> list[str]:
    if requested:
        return requested
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT {entity_col} FROM {std_table} ORDER BY {entity_col}")
        return [row[0] for row in cur.fetchall()]


def _load_prices(price_table: str, tickers: list[str]) -> dict[str, dict]:
    if not tickers:
        return {}
    query_tickers = set(tickers)
    aliases: dict[str, set[str]] = defaultdict(set)
    for ticker in tickers:
        aliases[ticker].add(ticker)
        if ticker.endswith(".T"):
            bare = ticker[:-2]
            query_tickers.add(bare)
            aliases[bare].add(ticker)
        else:
            jp_alias = f"{ticker}.T"
            query_tickers.add(jp_alias)
            aliases[jp_alias].add(ticker)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT ticker, date, close, return, log_return
            FROM {price_table}
            WHERE ticker = ANY(%s)
              AND close IS NOT NULL
            ORDER BY ticker, date
            """,
            (sorted(query_tickers),),
        )
        out = defaultdict(lambda: {"close": {}, "return": {}, "log_return": {}})
        for ticker, dt, close, ret, log_ret in cur.fetchall():
            for out_ticker in aliases.get(ticker, {ticker}):
                out[out_ticker]["close"][dt] = float(close)
                if ret is not None:
                    out[out_ticker]["return"][dt] = float(ret)
                if log_ret is not None:
                    out[out_ticker]["log_return"][dt] = float(log_ret)
        return out


def _load_split_events(jurisdiction: str, tickers: list[str]) -> dict[str, list[tuple[date, float]]]:
    if not tickers:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, effective_date, split_ratio
            FROM fact_stock_split_event
            WHERE jurisdiction = %s
              AND ticker = ANY(%s)
              AND split_ratio IS NOT NULL
            ORDER BY ticker, effective_date
            """,
            (jurisdiction, tickers),
        )
        out: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for ticker, effective_date, split_ratio in cur.fetchall():
            ratio = float(split_ratio)
            if ratio > 0 and not math.isnan(ratio) and not math.isinf(ratio):
                out[ticker].append((effective_date, ratio))
        return out


_FACTOR_MODELS = ("FF3", "FF4", "FF5", "FF6")
_GENERIC_FACTOR_MODEL_PRIORITY = ("FF6", "FF5")


def _load_factor_data(jurisdiction: str, tickers: list[str]) -> dict[str, dict]:
    if not tickers:
        return {}
    out: dict[str, dict] = defaultdict(lambda: {"summary": defaultdict(list), "implied": defaultdict(dict)})
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.ticker, l.model, l.window_end, l.beta_mkt, m.residual_vol
            FROM fact_factor_loadings l
            JOIN fact_factor_reg_meta m
              ON m.jurisdiction = l.jurisdiction
             AND m.ticker = l.ticker
             AND m.window_end = l.window_end
             AND m.model = l.model
            WHERE l.jurisdiction = %s
              AND l.ticker = ANY(%s)
              AND l.model IN ('FF3','FF4','FF5','FF6')
            ORDER BY l.ticker, l.model, l.window_end
            """,
            (jurisdiction, tickers),
        )
        for ticker, model, window_end, beta_mkt, residual_vol in cur.fetchall():
            out[ticker]["summary"][model].append(
                {
                    "window_end": window_end,
                    "beta_mkt": float(beta_mkt) if beta_mkt is not None else None,
                    "residual_vol": float(residual_vol) if residual_vol is not None else None,
                }
            )
        cur.execute(
            """
            SELECT ticker, model, date, implied_return
            FROM fact_factor_implied_returns
            WHERE jurisdiction = %s
              AND ticker = ANY(%s)
              AND model IN ('FF3','FF4','FF5','FF6')
              AND implied_return IS NOT NULL
            ORDER BY ticker, model, date
            """,
            (jurisdiction, tickers),
        )
        for ticker, model, dt, implied_return in cur.fetchall():
            out[ticker]["implied"][model][dt] = float(implied_return)
    return out


def _split_factor(events: list[tuple[date, float]], period_end: date | None, as_of: date | None) -> float:
    if not events or period_end is None or as_of is None:
        return 1.0
    factor = 1.0
    for effective_date, ratio in events:
        if period_end < effective_date <= as_of:
            factor *= ratio
    return factor


def _split_adjust_l0(values: dict[str, Any], factor: float) -> dict[str, Any]:
    if not values or factor == 1.0:
        return dict(values)
    out = dict(values)
    for item in _PER_SHARE_LINE_ITEMS:
        if item in out and out[item] is not None:
            out[item] = _div(out[item], factor)
    for item in _SHARE_COUNT_LINE_ITEMS:
        if item in out and out[item] is not None:
            adjusted = _f(out[item])
            out[item] = adjusted * factor if adjusted is not None else out[item]
    return out


def _price_at(close_map: dict[date, float], period_end: date | None) -> float | None:
    if not close_map or period_end is None:
        return None
    candidates = [dt for dt in close_map if dt <= period_end]
    if not candidates:
        return None
    return close_map[max(candidates)]


def _shares_for_market_cap(values: dict[str, Any]) -> float | None:
    for item in (
        "diluted_shares",
        "shares_outstanding_diluted",
        "shares_outstanding_basic",
        "shares_outstanding",
    ):
        shares = _f(_get_alias(values, item))
        if shares and shares > 0:
            return shares
    return None


def _price_metrics(ret_map: dict[date, float], period_end: date | None) -> dict[str, float]:
    if not ret_map or period_end is None:
        return {}
    out: dict[str, float] = {}

    def _compound(start: date, end: date) -> float | None:
        rets = [ret_map[dt] for dt in ret_map if start <= dt <= end and ret_map[dt] is not None]
        if len(rets) < 20:
            return None
        total = 1.0
        for ret in rets:
            total *= 1.0 + float(ret)
        value = total - 1.0
        return None if math.isnan(value) or math.isinf(value) else value

    trailing_dates = sorted(dt for dt in ret_map if dt <= period_end)[-252:]
    rets = [ret_map[dt] for dt in trailing_dates if ret_map[dt] is not None]
    if len(rets) >= 126:
        try:
            vol = stdev(rets) * math.sqrt(252)
            out["total_vol"] = vol
            out["total_volatility_252_day"] = vol
        except Exception:
            pass
    six_month = _compound(period_end - timedelta(days=183), period_end)
    if six_month is not None:
        out["price_momentum_6_month"] = six_month
    trailing_12 = _compound(period_end - timedelta(days=365), period_end)
    trailing_1 = _compound(period_end - timedelta(days=31), period_end)
    if trailing_12 is not None and trailing_1 is not None:
        out["price_momentum_12_month_minus_1_month"] = trailing_12 - trailing_1
    return out


def _factor_summary_at(factor_data: dict, model: str, period_end: date | None) -> dict | None:
    rows = list(factor_data.get("summary", {}).get(model, []))
    if not rows:
        return None
    eligible = [row for row in rows if period_end is None or row["window_end"] <= period_end]
    return (eligible or rows)[-1]


def _cum_simple_return(ret_map: dict[date, float], filed_date: date | None, days: int = 5) -> float | None:
    if not ret_map or filed_date is None:
        return None
    dates = sorted(dt for dt in ret_map if dt > filed_date)[:days]
    if len(dates) < days:
        return None
    total = 1.0
    for dt in dates:
        ret = ret_map.get(dt)
        if ret is None:
            return None
        total *= 1.0 + float(ret)
    value = total - 1.0
    return None if math.isnan(value) or math.isinf(value) else value


def _cum_residual_return(log_ret_map: dict[date, float], implied_map: dict[date, float], filed_date: date | None, days: int = 5) -> float | None:
    if not log_ret_map or not implied_map or filed_date is None:
        return None
    dates = sorted(dt for dt in log_ret_map if dt > filed_date and dt in implied_map)[:days]
    if len(dates) < days:
        return None
    total_log = 0.0
    for dt in dates:
        total_log += float(log_ret_map[dt]) - float(implied_map[dt])
    value = math.exp(total_log) - 1.0
    return None if math.isnan(value) or math.isinf(value) else value


def _namespace(l0, l1, prev_l0, prev_l1, l0_3y, l1_3y, l0_5y, l1_5y, market_cap, price, price_metrics):
    ns = {}
    ns.update(l0)
    ns.update(l1)
    for source, suffix in (
        (prev_l0, "_prev"), (prev_l1, "_prev"),
        (l0_3y, "_3y"), (l1_3y, "_3y"),
        (l0_5y, "_5y"), (l1_5y, "_5y"),
    ):
        for key, value in source.items():
            ns[key + suffix] = value
    ns["market_cap"] = market_cap
    ns[_MARKET_CAP_ID] = market_cap
    ns["price"] = price
    ns[_PRICE_ID] = price
    ns.update(price_metrics)
    return ns


def _compute_for_entity(entity_id: str, entity_ts: dict, entity_meta: dict, price_data: dict, split_events: list[tuple[date, float]], factor_data: dict, tickers: list[str], entity_id_type: str, jurisdiction: str, metric_meta: dict, order: list[str]) -> list[tuple]:
    keys = sorted(entity_ts.keys())
    fy_annual = {fy: key for fy, key in ((key[0], key) for key in keys if key[1] in _ANNUAL_FP)}
    l1_ts: dict[tuple, dict] = {}
    rows: list[tuple] = []
    close_map = price_data.get("close", {})
    ret_map = price_data.get("return", {})
    log_ret_map = price_data.get("log_return", {})
    period_ends = [entity_meta.get(key, (None, None, jurisdiction, None))[0] for key in keys]
    as_of = max((pe for pe in period_ends if pe is not None), default=None)
    adjusted_ts = {
        key: _split_adjust_l0(entity_ts[key], _split_factor(split_events, entity_meta.get(key, (None, None, jurisdiction, None))[0], as_of))
        for key in keys
    }

    for key in keys:
        fy, fp = key
        reported_l0 = entity_ts[key]
        l0 = adjusted_ts[key]
        pe, _currency, _jur, _filed_date = entity_meta.get(key, (None, None, jurisdiction, None))
        px = _price_at(close_map, pe)
        shares = _shares_for_market_cap(reported_l0)
        market_cap = px * shares if px and shares and shares > 0 else None
        prev_key = (fy - 1, fp) if (fy - 1, fp) in adjusted_ts else fy_annual.get(fy - 1)
        ns = dict(l0)
        for item, val in adjusted_ts.get(prev_key, {}).items():
            ns[item + "_prev"] = val
        for item, val in l1_ts.get(prev_key, {}).items():
            ns[item + "_prev"] = val
        ns["market_cap"] = market_cap
        ns[_MARKET_CAP_ID] = market_cap
        ns["price"] = px
        ns[_PRICE_ID] = px
        l1 = {}
        for node in order:
            if node not in _L1_FORMULAS or node in l0:
                continue
            val = _eval_formula(_L1_FORMULAS[node], {**ns, **l1})
            if val is not None:
                l1[node] = val
        l1_ts[key] = l1

    for key in keys:
        fy, fp = key
        reported_l0 = entity_ts[key]
        l0 = adjusted_ts[key]
        l1 = l1_ts.get(key, {})
        pe, currency, jur, filed_date = entity_meta.get(key, (None, None, jurisdiction, None))
        px = _price_at(close_map, pe)
        shares = _shares_for_market_cap(reported_l0)
        market_cap = px * shares if px and shares and shares > 0 else None
        prev_key = (fy - 1, fp) if (fy - 1, fp) in adjusted_ts else fy_annual.get(fy - 1)
        key_3y = fy_annual.get(fy - 3)
        key_5y = fy_annual.get(fy - 5)
        ns = _namespace(
            l0, l1,
            adjusted_ts.get(prev_key, {}), l1_ts.get(prev_key, {}),
            adjusted_ts.get(key_3y, {}) if key_3y else {}, l1_ts.get(key_3y, {}) if key_3y else {},
            adjusted_ts.get(key_5y, {}) if key_5y else {}, l1_ts.get(key_5y, {}) if key_5y else {},
            market_cap, px, _price_metrics(ret_map, pe),
        )
        ns = _with_legacy_aliases(ns)
        computed: dict[str, float] = {}
        fallback: dict[str, bool] = {}
        for node in order:
            if node not in _METRIC_FORMULAS:
                continue
            val = _eval_formula(_METRIC_FORMULAS[node], {**ns, **computed})
            if val is None:
                continue
            computed[node] = val
            req = _METRIC_PRIMARY_REQUIRED.get(node)
            fallback[node] = any(ns.get(item) is None for item in req) if req else False
        for metric_id in metric_meta:
            if metric_id in l1 and metric_id not in computed:
                computed[metric_id] = l1[metric_id]
                fallback[metric_id] = False
        if market_cap is not None and "market_cap" in metric_meta:
            computed["market_cap"] = market_cap
            fallback["market_cap"] = False
        prev_ns = {
            **adjusted_ts.get(prev_key, {}),
            **l1_ts.get(prev_key, {}),
        } if prev_key else {}
        prev_ns = _with_legacy_aliases(prev_ns)
        special, special_fallback = _compute_special(ns, prev_ns)
        computed.update({_METRIC_RENAMES.get(key, key): value for key, value in special.items()})
        fallback.update({_METRIC_RENAMES.get(key, key): value for key, value in special_fallback.items()})
        for metric_id, value in _price_metrics(ret_map, pe).items():
            metric_id = _METRIC_RENAMES.get(metric_id, metric_id)
            if metric_id in metric_meta and value is not None:
                computed[metric_id] = value
                fallback[metric_id] = False
        generic_factor_summary = None
        for model in _FACTOR_MODELS:
            summary = _factor_summary_at(factor_data, model, pe)
            suffix = model.lower()
            if summary:
                if summary.get("residual_vol") is not None:
                    metric_id = f"idiosyncratic_volatility_{suffix}"
                    if metric_id in metric_meta:
                        computed[metric_id] = summary["residual_vol"]
                        fallback[metric_id] = False
                if generic_factor_summary is None and model in _GENERIC_FACTOR_MODEL_PRIORITY:
                    generic_factor_summary = summary
        for model in _GENERIC_FACTOR_MODEL_PRIORITY:
            summary = _factor_summary_at(factor_data, model, pe)
            if summary:
                generic_factor_summary = summary
                break
        if generic_factor_summary:
            if generic_factor_summary.get("residual_vol") is not None and "idiosyncratic_volatility" in metric_meta:
                computed["idiosyncratic_volatility"] = generic_factor_summary["residual_vol"]
                fallback["idiosyncratic_volatility"] = False
            if generic_factor_summary.get("beta_mkt") is not None and "market_beta" in metric_meta:
                computed["market_beta"] = generic_factor_summary["beta_mkt"]
                fallback["market_beta"] = False
        raw_pead = _cum_simple_return(ret_map, filed_date)
        if raw_pead is not None:
            for metric_id in ("post_earnings_announcement_drift_raw", "post_earnings_announcement_drift"):
                if metric_id in metric_meta:
                    computed[metric_id] = raw_pead
                    fallback[metric_id] = False
        for model in _FACTOR_MODELS:
            residual_pead = _cum_residual_return(log_ret_map, factor_data.get("implied", {}).get(model, {}), filed_date)
            metric_id = f"post_earnings_announcement_drift_residual_{model.lower()}"
            if residual_pead is not None and metric_id in metric_meta:
                computed[metric_id] = residual_pead
                fallback[metric_id] = False
        for metric_id, value in computed.items():
            if metric_id not in metric_meta:
                continue
            category, importance, unit_type, metric_type, formula = metric_meta[metric_id]
            formula_expr = _METRIC_FORMULAS.get(metric_id) or _L1_FORMULAS.get(metric_id) or (
                f"{_PRICE_ID} * {_DILUTED_SHARES_ID}" if metric_id == _MARKET_CAP_ID else formula
            )
            for ticker in tickers:
                rows.append((
                    ticker, entity_id, entity_id, entity_id_type, jur or jurisdiction,
                    fy, fp, pe, metric_id, formula_expr, metric_type, category, importance,
                    unit_type, value, currency, fallback.get(metric_id, False),
                ))
    return rows


def _upsert(table: str, entity_col: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {table}
            (ticker, {entity_col}, primary_id, primary_id_type, jurisdiction,
             fiscal_year, fiscal_period, period_end, metric_id, formula, metric_type,
             category, importance, unit_type, value, currency, fallback_applied)
        VALUES %s
        ON CONFLICT (ticker, {entity_col}, fiscal_year, fiscal_period, metric_id)
        DO UPDATE SET
            primary_id = EXCLUDED.primary_id,
            primary_id_type = EXCLUDED.primary_id_type,
            jurisdiction = EXCLUDED.jurisdiction,
            period_end = EXCLUDED.period_end,
            formula = EXCLUDED.formula,
            metric_type = EXCLUDED.metric_type,
            category = EXCLUDED.category,
            importance = EXCLUDED.importance,
            unit_type = EXCLUDED.unit_type,
            value = EXCLUDED.value,
            currency = EXCLUDED.currency,
            fallback_applied = EXCLUDED.fallback_applied,
            computed_at = now()
    """
    with connect() as conn, conn.cursor() as cur:
        return execute_values(cur, sql, rows, page_size=5000)


def _clear(table: str, entity_col: str, entity_ids: list[str] | None) -> None:
    with connect() as conn, conn.cursor() as cur:
        if entity_ids:
            cur.execute(f"DELETE FROM {table} WHERE {entity_col} = ANY(%s)", (entity_ids,))
        else:
            cur.execute(f"TRUNCATE {table}")


def _metrics_is_fresh(std_table: str, out_table: str) -> bool:
    """True iff `out_table.computed_at` covers `std_table.updated_at`.

    NULL handling:
    - Empty std → nothing upstream to derive from, treated as fresh.
    - Empty out with non-empty std → not fresh, must run.

    Prices are intentionally NOT part of this gate: they update daily and would
    keep tripping a full metrics recompute. Use --full or per-entity scope to
    force a refresh when prices have moved.
    """
    # Compare inside SQL to avoid tz-aware vs naive timestamp issues across
    # tables (updated_at is TIMESTAMPTZ, computed_at is TIMESTAMP).
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH s AS (SELECT max(updated_at)  AT TIME ZONE 'UTC' AS t FROM {std_table}),
                 o AS (SELECT max(computed_at)                     AS t FROM {out_table})
            SELECT
                (SELECT t FROM s) IS NULL                 AS std_empty,
                (SELECT t FROM o) IS NULL                 AS out_empty,
                COALESCE((SELECT t FROM o) >= (SELECT t FROM s), FALSE) AS fresh
            """
        )
        std_empty, out_empty, fresh = cur.fetchone()
    if std_empty:
        return True
    if out_empty:
        return False
    return bool(fresh)


def compute_metrics(jurisdiction: str, entity_ids: list[str] | None = None, full: bool = False) -> int:
    cfg = {
        "US": ("fact_fundamentals_std_us", "fact_prices_us", "fact_metrics_us", "cik", "CIK"),
        "JP": ("fact_fundamentals_std_jp", "fact_prices_jp", "fact_metrics_jp", "edinet_code", "EDINET_CODE"),
    }[jurisdiction]
    std_table, price_table, out_table, entity_col, entity_id_type = cfg
    ctx = start_run(jurisdiction, "metrics", "full_refresh" if full else "incremental")
    try:
        if not full and not entity_ids and _metrics_is_fresh(std_table, out_table):
            print(f"{jurisdiction} metrics: skipped ({out_table} already covers {std_table})", flush=True)
            finish_run(ctx, "succeeded", rows_in=0, rows_out=0)
            return 0
        metric_meta = _load_metric_meta()
        order = _topo_order()
        entities = _entities(std_table, entity_col, entity_ids, full)
        if full or entity_ids:
            _clear(out_table, entity_col, entity_ids)
        tickers_by_entity = _load_entity_tickers(entity_id_type)
        all_tickers = sorted({ticker for entity in entities for ticker in tickers_by_entity.get(entity, [])})
        prices = _load_prices(price_table, all_tickers)
        split_events = _load_split_events(jurisdiction, all_tickers)
        factor_data = _load_factor_data(jurisdiction, all_tickers)
        ts, meta = _load_std_facts(std_table, entity_col, entities)
        rows: list[tuple] = []
        for entity_id in entities:
            tickers = tickers_by_entity.get(entity_id, [])
            if not tickers or entity_id not in ts:
                continue
            primary = tickers[0]
            rows.extend(
                _compute_for_entity(
                    entity_id, ts[entity_id], meta[entity_id],
                    prices.get(primary, {"close": {}, "return": {}}),
                    split_events.get(primary, []),
                    factor_data.get(primary, {}),
                    tickers, entity_id_type, jurisdiction, metric_meta, order,
                )
            )
        written = _upsert(out_table, entity_col, rows)
        finish_run(ctx, "succeeded", rows_in=len(entities), rows_out=written)
        return written
    except Exception as exc:
        finish_run(ctx, "failed", error=str(exc))
        raise
