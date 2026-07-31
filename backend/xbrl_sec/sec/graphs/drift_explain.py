"""Drift-Explain-Agent für Cross-Source-Reconciliation (SEC vs EDINET).

Bekommt eine Drift-Zeile (z.B. Toyota 7203 JP vs TM ADR US), die der
deterministische `cross_source_recon`-Knoten erzeugt hat. Versucht via DeepSeek-
Agent zu klassifizieren, ob die Drift erklärbar ist:

  - fx_translation: durch JPY/USD-Rate erklärbar → auto_accept
  - period_difference: Fiscal-Year-End-Misalignment → auto_accept
  - accounting_standard_difference: US-GAAP vs JP-GAAP → human_review
  - scope_difference: konsolidierter vs unkonsolidierter Abschluss → human_review
  - data_quality_issue: Pipeline-Bug → halt_pipeline
  - unexplained: keiner der Tools liefert eine Signatur → human_review

Output: `DriftClassification`, persistiert in `sec.drift_classification`.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from langchain_core.tools import tool

from xbrl_sec.llm import ChatDeepSeek
from xbrl_sec.llm.schemas import DriftClassification
from xbrl_sec.sec.db.connection import connect


@tool
def fetch_filing_section(filing_id: str, item: str = "MDA") -> dict[str, Any]:
    """Return a stored filing-section extract for the given filing_id and item.

    Used so the agent can read MD&A or Risk-Factor language that may hint at a
    one-off event behind the drift (e.g. divestment, currency hedge wind-down).
    """
    sql = """
        SELECT summary, key_risks, sentiment, extracted_at
        FROM sec.filing_section_extract
        WHERE filing_id = %s
          AND lower(item) LIKE %s
        ORDER BY extracted_at DESC
        LIMIT 1
    """
    pattern = f"%{item.lower()}%"
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (filing_id, pattern))
            row = cur.fetchone()
            if not row:
                return {"filing_id": filing_id, "item": item, "found": False}
            return {
                "filing_id": filing_id,
                "item": item,
                "found": True,
                "summary": row[0],
                "key_risks": row[1] or [],
                "sentiment": row[2],
                "extracted_at": row[3].isoformat() if row[3] else None,
            }
    except Exception as exc:  # noqa: BLE001
        return {"filing_id": filing_id, "item": item, "error": str(exc)[:200]}


@tool
def fx_rate_at_period_end(currency: str, period_end: str) -> dict[str, Any]:
    """Return the closing FX rate vs USD for the given currency on the given date."""
    sql = """
        SELECT rate_date, rate
        FROM fact_fx_rates
        WHERE currency = %s
          AND rate_date <= %s::date
        ORDER BY rate_date DESC
        LIMIT 1
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (currency.upper(), period_end))
            row = cur.fetchone()
            if not row:
                return {"currency": currency, "period_end": period_end, "found": False}
            return {
                "currency": currency.upper(),
                "period_end": period_end,
                "rate_date": row[0].isoformat() if row[0] else None,
                "rate": float(row[1] or 0),
                "found": True,
            }
    except Exception as exc:  # noqa: BLE001
        return {"currency": currency, "period_end": period_end, "error": str(exc)[:200]}


@tool
def period_calendar_lookup(cik: str | None = None, edinet_code: str | None = None) -> dict[str, Any]:
    """Return the filer's fiscal-year-end so the agent can spot calendar offsets."""
    if cik:
        sql = "SELECT cik, fiscal_year_end_month, fiscal_year_end_day FROM dim_company_us WHERE cik = %s"
        params = (cik,)
    elif edinet_code:
        sql = "SELECT edinet_code, fiscal_year_end_month, fiscal_year_end_day FROM dim_company_jp WHERE edinet_code = %s"
        params = (edinet_code,)
    else:
        return {"error": "must provide cik or edinet_code"}
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if not row:
                return {"found": False}
            return {
                "identifier": row[0],
                "fiscal_year_end_month": int(row[1]) if row[1] is not None else None,
                "fiscal_year_end_day": int(row[2]) if row[2] is not None else None,
                "found": True,
            }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def _persist(classification: DriftClassification) -> None:
    sql = """
        INSERT INTO sec.drift_classification
            (cik, edinet_code, period_end, concept, reason, action,
             confidence, rationale, fx_rate_used)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (cik, edinet_code, period_end, concept) DO UPDATE SET
            reason = EXCLUDED.reason,
            action = EXCLUDED.action,
            confidence = EXCLUDED.confidence,
            rationale = EXCLUDED.rationale,
            fx_rate_used = EXCLUDED.fx_rate_used,
            decided_at = NOW()
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    classification.cik,
                    classification.edinet_code,
                    classification.period_end,
                    classification.concept,
                    classification.reason,
                    classification.action,
                    classification.confidence,
                    classification.rationale[:600],
                    classification.fx_rate_used,
                ),
            )
    except Exception:
        pass


_INSTRUCTIONS = (
    "You are auditing a cross-source reconciliation drift between an SEC US "
    "filing and an EDINET JP filing for the same group (e.g. Toyota 7203 JP / "
    "TM ADR US). Use tools to inspect FX rates at the period end, the filer's "
    "fiscal-year-end calendar, and any extracted MD&A summary. Decide whether "
    "the drift is auto_accept (FX or period offset cleanly explains it), "
    "auto_correct (clear FX adjustment we can apply), human_review (genuine "
    "accounting/scope difference), or halt_pipeline (data corruption). Return "
    "exactly one DriftClassification."
)


def _build_agent(llm: ChatDeepSeek | None) -> ChatDeepSeek:
    llm = llm or ChatDeepSeek(model="deepseek-v4-flash", temperature=0.1, max_tokens=1500)
    llm.bind_tools([fetch_filing_section, fx_rate_at_period_end, period_calendar_lookup])
    return llm


def explain_drift(
    *,
    cik: str | None,
    edinet_code: str | None,
    period_end: str,
    concept: str,
    us_value: float | None,
    jp_value: float | None,
    drift_pct: float,
    filing_id_us: str | None = None,
    filing_id_jp: str | None = None,
    llm: ChatDeepSeek | None = None,
) -> DriftClassification:
    """Run the agent on a single drift row and persist the classification."""
    chat = _build_agent(llm)
    structured = chat.with_structured_output(DriftClassification)
    prompt = (
        f"{_INSTRUCTIONS}\n\n"
        f"cik: {cik}\n"
        f"edinet_code: {edinet_code}\n"
        f"period_end: {period_end}\n"
        f"concept: {concept}\n"
        f"us_value: {us_value}\n"
        f"jp_value: {jp_value}\n"
        f"drift_pct: {drift_pct:.4f}\n"
        f"filing_id_us: {filing_id_us}\n"
        f"filing_id_jp: {filing_id_jp}\n"
    )
    try:
        classification = structured.invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        classification = DriftClassification(
            cik=cik,
            edinet_code=edinet_code,
            period_end=period_end,
            concept=concept,
            reason="unexplained",
            action="human_review",
            confidence=0.0,
            rationale=f"LLM error: {exc}",
        )
    if classification.cik != cik or classification.edinet_code != edinet_code or classification.concept != concept:
        classification = classification.model_copy(
            update={"cik": cik, "edinet_code": edinet_code, "period_end": period_end, "concept": concept}
        )
    _persist(classification)
    return classification


def explain_drift_batch(rows: list[dict[str, Any]], llm: ChatDeepSeek | None = None) -> dict[str, int]:
    auto_accept = auto_correct = human = halt = 0
    for row in rows:
        result = explain_drift(
            cik=row.get("cik"),
            edinet_code=row.get("edinet_code"),
            period_end=row["period_end"],
            concept=row["concept"],
            us_value=row.get("us_value"),
            jp_value=row.get("jp_value"),
            drift_pct=float(row.get("drift_pct") or 0.0),
            filing_id_us=row.get("filing_id_us"),
            filing_id_jp=row.get("filing_id_jp"),
            llm=llm,
        )
        if result.action == "auto_accept":
            auto_accept += 1
        elif result.action == "auto_correct":
            auto_correct += 1
        elif result.action == "human_review":
            human += 1
        else:
            halt += 1
    return {
        "rows": len(rows),
        "auto_accept": auto_accept,
        "auto_correct": auto_correct,
        "human_review": human,
        "halt_pipeline": halt,
    }


def detect_cross_source_drift(threshold_pct: float = 0.05, limit: int = 25) -> list[dict[str, Any]]:
    """Find dual-listed filers (same group) with metric drift above threshold.

    Looks at the security_identifier_us mapping to find filers with both a CIK
    and an EDINET code, then compares the latest matching quarter's revenue.
    """
    sql = """
        WITH us_latest AS (
            SELECT m.cik, m.period_end, m.value AS us_value
            FROM fact_metrics_us m
            WHERE m.metric_id = 'Revenue'
              AND m.period_end >= CURRENT_DATE - INTERVAL '400 days'
        ),
        jp_latest AS (
            SELECT m.edinet_code, m.period_end, m.value AS jp_value
            FROM fact_metrics_jp m
            WHERE m.metric_id = 'Revenue'
              AND m.period_end >= CURRENT_DATE - INTERVAL '400 days'
        )
        SELECT j.cik, j.edinet_code, u.period_end,
               u.us_value, p.jp_value
        FROM security_identifier_us j
        JOIN us_latest u ON u.cik = j.cik
        JOIN jp_latest p ON p.edinet_code = j.edinet_code AND p.period_end = u.period_end
        WHERE j.cik IS NOT NULL AND j.edinet_code IS NOT NULL
          AND u.us_value > 0 AND p.jp_value > 0
          AND abs(p.jp_value - u.us_value) / GREATEST(u.us_value, 1) >= %s
        LIMIT %s
    """
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (threshold_pct, limit))
            rows = cur.fetchall()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for cik, edinet_code, period_end, us_value, jp_value in rows:
        drift_pct = abs(float(jp_value) - float(us_value)) / max(float(us_value), 1.0)
        out.append(
            {
                "cik": cik,
                "edinet_code": edinet_code,
                "period_end": period_end.isoformat() if isinstance(period_end, date) else str(period_end),
                "concept": "Revenue",
                "us_value": float(us_value),
                "jp_value": float(jp_value),
                "drift_pct": drift_pct,
            }
        )
    return out
