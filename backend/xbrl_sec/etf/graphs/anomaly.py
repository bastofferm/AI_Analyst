"""Holdings anomaly detector for the ETF daily graph.

Deterministische Vorprüfung + optionaler LLM-Erklärungsschritt:

1. Für jede ETF mit kürzlich geschriebenen Holdings vergleichen wir den
   neuesten Snapshot-Tag mit dem 30-Tage-Median.
2. Wenn die Anzahl der Holdings, die Top-1-Gewichtung oder die kumulierte
   Gewichtsänderung die Schwellen überschreiten, wird ein Anomalie-Befund
   erzeugt.
3. Bei jeder Anomalie ruft ein optionaler LLM-Step DeepSeek auf, um eine
   Hypothese und Handlungsempfehlung zu liefern (Schema: HoldingsAnomaly).
4. severity='high' + LLM-Vorschlag 'human_review' triggert später das
   interrupt() im Approval-Gate.

Anti-Pattern, das wir bewusst vermeiden: alle ETFs durch das LLM zu jagen.
Der LLM-Call lohnt sich nur, wenn die Heuristik bereits eine Drift gemeldet
hat.
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Iterable

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import HoldingsAnomaly
from xbrl_sec.sec.db.connection import connect


_DEFAULT_LOOKBACK_DAYS = 30
_HOLDINGS_COUNT_DROP_PCT = 0.20
_TOP_WEIGHT_JUMP_PCT = 0.10


def _fetch_holdings_history(isin: str, lookback_days: int) -> list[tuple[date, int, float]]:
    """Return (as_of_date, holdings_count, top_weight) trail for the last N days."""
    sql = """
        SELECT as_of_date,
               count(*) AS holdings_count,
               COALESCE(MAX(weight), 0) AS top_weight
        FROM sec.etf_holding
        WHERE isin = %s
          AND as_of_date >= %s
        GROUP BY as_of_date
        ORDER BY as_of_date
    """
    cutoff = date.today() - timedelta(days=lookback_days)
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (isin, cutoff))
            return [(row[0], int(row[1]), float(row[2] or 0.0)) for row in cur.fetchall()]
    except Exception:
        return []


def _deterministic_findings(history: list[tuple[date, int, float]]) -> dict[str, float] | None:
    """Liefert die deterministischen Metriken, falls die Schwellen verletzt sind."""
    if len(history) < 5:
        return None
    counts = [row[1] for row in history]
    top_weights = [row[2] for row in history]
    latest_count = counts[-1]
    latest_top = top_weights[-1]
    median_count = statistics.median(counts[:-1])
    median_top = statistics.median(top_weights[:-1])
    if median_count <= 0:
        return None
    count_drop = (median_count - latest_count) / median_count
    top_jump = latest_top - median_top

    if count_drop >= _HOLDINGS_COUNT_DROP_PCT or abs(top_jump) >= _TOP_WEIGHT_JUMP_PCT:
        return {
            "median_holdings_count": float(median_count),
            "latest_holdings_count": float(latest_count),
            "holdings_count_drop_pct": float(count_drop),
            "median_top_weight": float(median_top),
            "latest_top_weight": float(latest_top),
            "top_weight_jump_pct": float(top_jump),
        }
    return None


_EXPLAIN_PROMPT = (
    "You are auditing daily ETF holdings refreshes. Given a metric anomaly, "
    "produce a JSON object that matches the HoldingsAnomaly schema. "
    "Severity rules: high if the data is implausible (>40% holdings drop), "
    "medium if 20-40%, low otherwise. Suggested_action: 'rerun_fetch' for "
    "transient provider issues, 'human_review' for plausible-but-significant "
    "changes, 'halt_pipeline' for clear data corruption, 'ignore' if the "
    "change is small and within noise.\n\n"
    "ISIN: {isin}\n"
    "Latest observation date: {latest_date}\n"
    "Deterministic metrics: {metrics}\n"
    "Recent holdings_count trail: {counts}\n"
)


def _llm_explain(
    isin: str,
    latest_date: date,
    metrics: dict[str, float],
    counts: Iterable[int],
    llm: ChatDeepSeek,
) -> HoldingsAnomaly | None:
    structured = llm.with_structured_output(HoldingsAnomaly)
    try:
        return structured.invoke(
            _EXPLAIN_PROMPT.format(
                isin=isin,
                latest_date=latest_date.isoformat(),
                metrics=metrics,
                counts=list(counts),
            )
        )
    except Exception:
        return None


def detect_holdings_anomalies(
    isins: list[str],
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    llm: ChatDeepSeek | None = None,
) -> list[HoldingsAnomaly]:
    """Scan the given ISINs and return one HoldingsAnomaly per offender.

    The LLM is only consulted when the deterministic threshold fires, which
    keeps the per-day call volume tightly bounded.
    """
    if not isins:
        return []
    llm = llm or ChatDeepSeek(model="deepseek-v4-flash", temperature=0.0, max_tokens=600)
    findings: list[HoldingsAnomaly] = []
    for isin in isins:
        history = _fetch_holdings_history(isin, lookback_days)
        metrics = _deterministic_findings(history)
        if metrics is None:
            continue
        latest_date = history[-1][0]
        counts = [row[1] for row in history]
        finding = _llm_explain(isin, latest_date, metrics, counts, llm)
        if finding is None:
            finding = HoldingsAnomaly(
                isin=isin,
                severity="medium",
                hypothesis="Deterministic threshold tripped; LLM unavailable for explanation.",
                suggested_action="human_review",
                metrics=metrics,
                confidence=0.5,
            )
        else:
            finding = finding.model_copy(update={"metrics": metrics})
        findings.append(finding)
    return findings
