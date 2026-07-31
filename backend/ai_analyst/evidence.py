from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from ._db import read_sql


EvidenceKind = Literal[
    "mda",
    "filing_section",
    "rich_filing_section",
    "news",
    "ownership",
    "macro",
    "statement",
    "recon",
    "yahoo",
    "data_quality",
]

Confidence = Literal["low", "medium", "high"]

MAX_RESPONSE_CARDS = 24
MAX_RESPONSE_TREES = 2
MAX_EXCERPT_CHARS = 600
MAX_COMPACT_CARDS = 12


class EvidenceSource(BaseModel):
    kind: EvidenceKind
    source_id: str
    label: str
    as_of: str | None = None
    uri: str | None = None
    source_path: str | None = None


class EvidenceCitation(BaseModel):
    citation_id: str
    source_id: str
    label: str | None = None
    quote: str | None = None
    section_id: str | None = None
    filing_id: str | None = None
    url: str | None = None
    char_offset: int | None = None


class EvidenceCard(BaseModel):
    card_id: str
    kind: EvidenceKind
    title: str
    summary: str
    excerpt: str | None = None
    as_of: str | None = None
    confidence: Confidence = "medium"
    source: EvidenceSource
    citations: list[EvidenceCitation] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvidenceTreeNode(BaseModel):
    node_id: str
    parent_id: str | None = None
    position: int = 0
    title: str
    summary: str
    content: str | None = None
    citations: list[EvidenceCitation] = Field(default_factory=list)
    children_ids: list[str] = Field(default_factory=list)


class EvidenceTree(BaseModel):
    tree_id: str
    kind: EvidenceKind
    title: str
    root_node_id: str
    nodes: dict[str, EvidenceTreeNode]
    as_of: str | None = None
    source: EvidenceSource


class EvidenceBundle(BaseModel):
    ticker: str
    jurisdiction: str
    cards: list[EvidenceCard] = Field(default_factory=list)
    trees: list[EvidenceTree] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False


def stable_evidence_id(*parts: object) -> str:
    joined = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return f"ev-{digest}"


def truncate_excerpt(value: object, limit: int = MAX_EXCERPT_CHARS) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def build_evidence_bundle(
    *,
    ticker: str,
    jurisdiction: str,
    entity_id: str | None = None,
    packet: dict[str, Any] | None = None,
    mda_text: str | None = None,
    segment_data: dict[str, Any] | None = None,
    rich_filing_sections: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
    ownership: dict[str, Any] | None = None,
    analytics: dict[str, Any] | None = None,
    data_quality_report: dict[str, Any] | None = None,
) -> EvidenceBundle:
    del segment_data, analytics
    cards: list[EvidenceCard] = []
    trees: list[EvidenceTree] = []
    warnings: list[str] = []
    packet = packet or {}
    entity = entity_id or ticker
    jurisdiction = (jurisdiction or "US").upper()

    try:
        mda_cards, mda_trees = _mda_evidence(ticker, jurisdiction, entity, mda_text)
        cards.extend(mda_cards)
        trees.extend(mda_trees)
        if not mda_cards:
            warnings.append("mda source empty")
    except Exception as exc:
        warnings.append(f"mda evidence unavailable: {exc}")

    try:
        filing_cards = _filing_section_evidence(ticker, jurisdiction, entity)
        cards.extend(filing_cards)
        if jurisdiction == "US" and not filing_cards:
            warnings.append("filing section source missing or empty: sec.filing_section_extract")
    except Exception as exc:
        warnings.append(f"filing section evidence unavailable: {exc}")

    try:
        rich_cards = _rich_filing_section_evidence(ticker, jurisdiction, entity, rich_filing_sections)
        cards.extend(rich_cards)
    except Exception as exc:
        warnings.append(f"rich filing section evidence unavailable: {exc}")

    try:
        statement_cards = _statement_evidence(ticker, jurisdiction, entity, packet)
        cards.extend(statement_cards)
        if not statement_cards:
            warnings.append("statement metadata empty in report packet")
    except Exception as exc:
        warnings.append(f"statement evidence unavailable: {exc}")

    try:
        yahoo_cards = _yahoo_evidence(ticker, jurisdiction, entity, packet)
        cards.extend(yahoo_cards)
    except Exception as exc:
        warnings.append(f"Yahoo cross-check evidence unavailable: {exc}")

    try:
        recon_cards = _recon_evidence(ticker, jurisdiction, entity, packet)
        cards.extend(recon_cards)
        if not recon_cards:
            warnings.append("reconciliation evidence empty in report packet")
    except Exception as exc:
        warnings.append(f"recon evidence unavailable: {exc}")

    try:
        cards.extend(_data_quality_evidence(ticker, jurisdiction, entity, data_quality_report))
    except Exception as exc:
        warnings.append(f"data-quality evidence unavailable: {exc}")

    cards.extend(_runtime_context_cards(ticker, jurisdiction, entity, macro, news, ownership))
    return _finalize_bundle(ticker, jurisdiction, cards, trees, warnings)


def merge_runtime_context(
    bundle: EvidenceBundle | dict[str, Any] | None,
    *,
    ticker: str | None = None,
    jurisdiction: str | None = None,
    macro: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
    ownership: dict[str, Any] | None = None,
) -> EvidenceBundle:
    base = _bundle_from_any(bundle, ticker=ticker, jurisdiction=jurisdiction)
    cards = [card for card in base.cards if card.kind not in {"macro", "news", "ownership"}]
    cards.extend(
        _runtime_context_cards(
            ticker or base.ticker,
            jurisdiction or base.jurisdiction,
            ticker or base.ticker,
            macro,
            news,
            ownership,
        )
    )
    return _finalize_bundle(base.ticker, base.jurisdiction, cards, base.trees, list(base.warnings))


def compact_evidence_bundle(
    bundle: EvidenceBundle | dict[str, Any] | None,
    *,
    max_cards: int = MAX_COMPACT_CARDS,
) -> dict[str, Any]:
    parsed = _bundle_from_any(bundle)
    cards = []
    for card in parsed.cards[:max_cards]:
        cards.append(
            {
                "evidence_id": card.card_id,
                "kind": card.kind,
                "title": card.title,
                "summary": truncate_excerpt(card.summary, 240),
                "as_of": card.as_of,
                "confidence": card.confidence,
                "citations": [
                    {
                        "citation_id": citation.citation_id,
                        "source_id": citation.source_id,
                        "label": citation.label,
                        "quote": truncate_excerpt(citation.quote or "", 240),
                    }
                    for citation in card.citations[:2]
                ],
            }
        )
    return {
        "cards": cards,
        "counts": parsed.counts,
        "warnings": parsed.warnings,
        "truncated": parsed.truncated or len(parsed.cards) > max_cards,
    }


def _bundle_from_any(
    bundle: EvidenceBundle | dict[str, Any] | None,
    *,
    ticker: str | None = None,
    jurisdiction: str | None = None,
) -> EvidenceBundle:
    if isinstance(bundle, EvidenceBundle):
        return bundle
    if isinstance(bundle, dict):
        try:
            return EvidenceBundle.model_validate(bundle)
        except Exception:
            pass
    return EvidenceBundle(ticker=ticker or "UNKNOWN", jurisdiction=(jurisdiction or "US").upper())


def _finalize_bundle(
    ticker: str,
    jurisdiction: str,
    cards: list[EvidenceCard],
    trees: list[EvidenceTree],
    warnings: list[str],
) -> EvidenceBundle:
    truncated = len(cards) > MAX_RESPONSE_CARDS or len(trees) > MAX_RESPONSE_TREES
    limited_cards = cards[:MAX_RESPONSE_CARDS]
    limited_trees = trees[:MAX_RESPONSE_TREES]
    counts = Counter(card.kind for card in limited_cards)
    return EvidenceBundle(
        ticker=ticker,
        jurisdiction=(jurisdiction or "US").upper(),
        cards=limited_cards,
        trees=limited_trees,
        counts=dict(sorted(counts.items())),
        warnings=list(dict.fromkeys(warnings)),
        truncated=truncated,
    )


def _mda_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    mda_text: str | None,
) -> tuple[list[EvidenceCard], list[EvidenceTree]]:
    excerpt = truncate_excerpt(mda_text or "")
    if not excerpt:
        return [], []

    source = EvidenceSource(
        kind="mda",
        source_id=stable_evidence_id("source", "mda", jurisdiction, entity),
        label="Latest MD&A text",
        source_path="fact_mda_sections",
    )
    citation = EvidenceCitation(
        citation_id=stable_evidence_id("cite", "mda", jurisdiction, entity, excerpt[:80]),
        source_id=source.source_id,
        label="MD&A excerpt",
        quote=excerpt,
    )
    card = EvidenceCard(
        card_id=stable_evidence_id("card", "mda", jurisdiction, entity, excerpt[:80]),
        kind="mda",
        title="Latest MD&A excerpt",
        summary=truncate_excerpt(excerpt, 240),
        excerpt=excerpt,
        confidence="medium",
        source=source,
        citations=[citation],
        tags=[ticker, "qualitative"],
    )

    root_id = stable_evidence_id("tree-root", "mda", jurisdiction, entity)
    child_id = stable_evidence_id("tree-node", "mda", jurisdiction, entity, excerpt[:80])
    root = EvidenceTreeNode(
        node_id=root_id,
        position=0,
        title="MD&A",
        summary="Latest available management discussion evidence.",
        children_ids=[child_id],
    )
    child = EvidenceTreeNode(
        node_id=child_id,
        parent_id=root_id,
        position=1,
        title="Extracted section",
        summary=truncate_excerpt(excerpt, 180),
        content=excerpt,
        citations=[citation],
    )
    tree = EvidenceTree(
        tree_id=stable_evidence_id("tree", "mda", jurisdiction, entity),
        kind="mda",
        title="MD&A section tree",
        root_node_id=root_id,
        nodes={root_id: root, child_id: child},
        source=source,
    )
    return [card], [tree]


def _filing_section_evidence(ticker: str, jurisdiction: str, entity: str) -> list[EvidenceCard]:
    if jurisdiction.upper() != "US" or not entity:
        return []
    if not _relation_exists("sec.filing_section_extract"):
        return _xbrl_mda_section_evidence(ticker, jurisdiction, entity)

    cik = str(entity)
    cik_raw = cik.lstrip("0") or cik
    df = read_sql(
        """
        SELECT filing_id, cik, item, text_excerpt, summary, key_risks, sentiment,
               model_version, extracted_at
        FROM sec.filing_section_extract
        WHERE cik IN (%(cik)s, %(cik_raw)s)
        ORDER BY extracted_at DESC NULLS LAST
        LIMIT 5
        """,
        {"cik": cik, "cik_raw": cik_raw},
    )
    cards: list[EvidenceCard] = []
    for index, row in enumerate(_records(df)):
        item = _clean_text(row.get("item") or f"Section {index + 1}")
        filing_id = _clean_text(row.get("filing_id") or "")
        as_of = _clean_text(row.get("extracted_at") or "")[:10] or None
        excerpt = truncate_excerpt(row.get("summary") or row.get("text_excerpt") or "")
        if not excerpt:
            continue
        source = EvidenceSource(
            kind="filing_section",
            source_id=stable_evidence_id("source", "filing_section", jurisdiction, entity, filing_id, item),
            label=f"Filing section {item}",
            as_of=as_of,
            source_path="sec.filing_section_extract",
        )
        citation = EvidenceCitation(
            citation_id=stable_evidence_id("cite", "filing_section", jurisdiction, entity, filing_id, item),
            source_id=source.source_id,
            label=item,
            quote=truncate_excerpt(row.get("text_excerpt") or row.get("summary") or ""),
            section_id=item,
            filing_id=filing_id or None,
        )
        cards.append(
            EvidenceCard(
                card_id=stable_evidence_id("card", "filing_section", jurisdiction, entity, filing_id, item),
                kind="filing_section",
                title=f"Filing section {item}",
                summary=excerpt,
                excerpt=truncate_excerpt(row.get("text_excerpt") or excerpt),
                as_of=as_of,
                confidence="medium",
                source=source,
                citations=[citation],
                tags=[ticker, "filing"],
            )
        )
    return cards or _xbrl_mda_section_evidence(ticker, jurisdiction, entity)


def _xbrl_mda_section_evidence(ticker: str, jurisdiction: str, entity: str) -> list[EvidenceCard]:
    cik = str(entity)
    cik_raw = cik.lstrip("0") or cik
    try:
        df = read_sql(
            """
            SELECT filing_id, section_id, form_type, filed_date, section_text,
                   char_count, extraction_method, extraction_quality
            FROM sec.fact_mda_sections_us
            WHERE cik IN (%(cik)s, %(cik_raw)s)
              AND section_id IN ('item_2', 'item_7')
              AND section_text IS NOT NULL
            ORDER BY filed_date DESC NULLS LAST,
                     CASE section_id WHEN 'item_2' THEN 0 WHEN 'item_7' THEN 1 ELSE 2 END,
                     char_count DESC NULLS LAST
            LIMIT 5
            """,
            {"cik": cik, "cik_raw": cik_raw},
        )
    except Exception:
        return []

    labels = {
        "item_2": "Item 2 MD&A",
        "item_7": "Item 7 MD&A",
    }
    cards: list[EvidenceCard] = []
    for row in _records(df):
        item = _clean_text(row.get("section_id") or "")
        label = labels.get(item, item or "MD&A")
        filing_id = _clean_text(row.get("filing_id") or "")
        filed_date = _clean_text(row.get("filed_date") or "")[:10] or None
        form_type = _clean_text(row.get("form_type") or "")
        excerpt = truncate_excerpt(row.get("section_text") or "")
        if not excerpt:
            continue
        source = EvidenceSource(
            kind="filing_section",
            source_id=stable_evidence_id("source", "xbrl_mda", jurisdiction, entity, filing_id, item),
            label=f"XBRL HTML {label}",
            as_of=filed_date,
            source_path="sec.fact_mda_sections_us",
        )
        citation = EvidenceCitation(
            citation_id=stable_evidence_id("cite", "xbrl_mda", jurisdiction, entity, filing_id, item),
            source_id=source.source_id,
            label=f"{form_type} {label}".strip(),
            quote=excerpt,
            section_id=item or None,
            filing_id=filing_id or None,
        )
        summary = f"{form_type} {label} filed {filed_date}: {truncate_excerpt(excerpt, 180)}".strip()
        cards.append(
            EvidenceCard(
                card_id=stable_evidence_id("card", "xbrl_mda", jurisdiction, entity, filing_id, item),
                kind="filing_section",
                title=f"XBRL HTML {label}",
                summary=truncate_excerpt(summary, 260),
                excerpt=excerpt,
                as_of=filed_date,
                confidence="high" if row.get("extraction_quality") == "clean" else "medium",
                source=source,
                citations=[citation],
                tags=[ticker, "filing", "xbrl-html", "mda"],
            )
        )
    return cards


def _rich_filing_section_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    rich_filing_sections: dict[str, Any] | None,
) -> list[EvidenceCard]:
    if jurisdiction.upper() != "US" or not isinstance(rich_filing_sections, dict):
        return []
    rows = [row for row in (rich_filing_sections.get("sections") or []) if isinstance(row, dict)]
    cards: list[EvidenceCard] = []
    for row in rows[:8]:
        family = _clean_text(row.get("section_family") or "rich_filing_section")
        title = _clean_text(row.get("section_title") or row.get("concept_name") or family)
        filing_id = _clean_text(row.get("filing_id") or row.get("accession_no") or "")
        concept = _clean_text(row.get("concept_name") or "")
        as_of = _clean_text(row.get("filing_date") or "")[:10] or None
        form = _clean_text(row.get("form_type") or "")
        score = row.get("quality_score")
        table_count = row.get("table_count")
        if table_count is None:
            table_count = len(row.get("tables_jsonb") or [])
        excerpt = truncate_excerpt(row.get("excerpt") or row.get("plain_text") or "")
        if not excerpt:
            continue
        source = EvidenceSource(
            kind="rich_filing_section",
            source_id=stable_evidence_id("source", "rich_filing_section", jurisdiction, entity, filing_id, concept),
            label=f"XBRL HTML {family.replace('_', ' ')}",
            as_of=as_of,
            source_path=row.get("source_html_path") or "sec.fact_rich_filing_sections_us",
        )
        score_text = f" score {score}" if score is not None else ""
        table_text = f"{table_count} embedded table(s)" if table_count else "tagged TextBlock"
        summary = row.get("summary") or f"{form} {title}: {table_text}{score_text}. {excerpt}"
        confidence: Confidence = "high" if isinstance(score, (int, float)) and score >= 60 else "medium"
        cards.append(
            EvidenceCard(
                card_id=stable_evidence_id("card", "rich_filing_section", jurisdiction, entity, filing_id, concept, row.get("text_hash")),
                kind="rich_filing_section",
                title=f"{form + ' ' if form else ''}{title}",
                summary=truncate_excerpt(summary, 260),
                excerpt=excerpt,
                as_of=as_of,
                confidence=confidence,
                source=source,
                citations=[
                    EvidenceCitation(
                        citation_id=stable_evidence_id("cite", "rich_filing_section", jurisdiction, entity, filing_id, concept),
                        source_id=source.source_id,
                        label=concept or title,
                        quote=excerpt,
                        section_id=family,
                        filing_id=filing_id or None,
                    )
                ],
                tags=[tag for tag in [ticker, "filing", "xbrl-html", family, row.get("sector_scope")] if tag],
            )
        )
    return cards


def _statement_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    packet: dict[str, Any],
) -> list[EvidenceCard]:
    rows = _packet_rows(packet, "modeled_statements")
    cards: list[EvidenceCard] = []
    for row in rows[:6]:
        fiscal_year = _clean_text(row.get("fiscal_year") or row.get("period") or row.get("year") or "")
        label = _clean_text(row.get("label") or row.get("target_variable") or row.get("statement") or "Modeled statement")
        value = row.get("value")
        source_concept = _clean_text(row.get("source_concept_id") or row.get("concept_id") or "")
        as_of = _clean_text(row.get("filed_at") or row.get("filing_date") or row.get("as_of") or "")[:10] or None
        summary = f"{label}"
        if fiscal_year:
            summary += f" ({fiscal_year})"
        if value not in (None, ""):
            summary += f": {value}"
        if source_concept:
            summary += f" from {source_concept}"

        source = EvidenceSource(
            kind="statement",
            source_id=stable_evidence_id("source", "statement", jurisdiction, entity, fiscal_year, label),
            label="Modeled financial statement",
            as_of=as_of,
            source_path="report_data_packet.modeled_statements",
        )
        cards.append(
            EvidenceCard(
                card_id=stable_evidence_id("card", "statement", jurisdiction, entity, fiscal_year, label, source_concept),
                kind="statement",
                title=label,
                summary=truncate_excerpt(summary, 260),
                excerpt=truncate_excerpt(summary),
                as_of=as_of,
                confidence="high",
                source=source,
                citations=[
                    EvidenceCitation(
                        citation_id=stable_evidence_id("cite", "statement", jurisdiction, entity, fiscal_year, label),
                        source_id=source.source_id,
                        label=source_concept or label,
                        quote=truncate_excerpt(summary),
                    )
                ],
                tags=[ticker, "numeric"],
            )
        )
    return cards


def _yahoo_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    packet: dict[str, Any],
) -> list[EvidenceCard]:
    check = packet.get("yahoo_cross_check") if isinstance(packet, dict) else None
    if not isinstance(check, dict) or not check:
        return []
    source_table = _clean_text(check.get("source_table") or "fact_yfinance_fundamental_snapshot")
    as_of = _clean_text(check.get("snapshot_date") or "")[:10] or None
    rows = [row for row in (check.get("rows") or []) if isinstance(row, dict)]
    ranked = sorted(
        rows,
        key=lambda row: (
            {"material": 0, "currency_mismatch": 1, "watch": 2, "informational": 3, "ok": 4}.get(
                _clean_text(row.get("severity")), 9
            ),
            _clean_text(row.get("line_item_id")),
        ),
    )
    worst = []
    for row in ranked[:5]:
        pct = row.get("pct_delta")
        pct_text = f"{float(pct):+.1f}%" if isinstance(pct, (int, float)) else "n/a"
        worst.append(f"{row.get('line_item_id')}: {row.get('severity')} ({pct_text})")
    summary = _clean_text(check.get("summary") or "")
    if worst:
        summary = f"{summary} Worst overlaps: {'; '.join(worst)}"
    if not summary:
        summary = _clean_text(check.get("note") or "Yahoo Finance cross-check unavailable.")
    source = EvidenceSource(
        kind="yahoo",
        source_id=stable_evidence_id("source", "yahoo", jurisdiction, entity, as_of),
        label="Yahoo Finance fundamentals cross-check",
        as_of=as_of,
        source_path=f"sec.{source_table}",
    )
    citation = EvidenceCitation(
        citation_id=stable_evidence_id("cite", "yahoo", jurisdiction, entity, as_of, summary[:80]),
        source_id=source.source_id,
        label="Yahoo vs SEC/EDINET reconciliation",
        quote=truncate_excerpt(summary),
    )
    return [
        EvidenceCard(
            card_id=stable_evidence_id("card", "yahoo", jurisdiction, entity, as_of, summary[:80]),
            kind="yahoo",
            title="Yahoo Finance cross-check",
            summary=truncate_excerpt(summary, 260),
            excerpt=truncate_excerpt(summary),
            as_of=as_of,
            confidence="medium" if check.get("available") else "low",
            source=source,
            citations=[citation],
            tags=[ticker, "data-quality", "yahoo"],
        )
    ]


def _recon_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    packet: dict[str, Any],
) -> list[EvidenceCard]:
    rows = _packet_rows(packet, "recon_flags")
    cards: list[EvidenceCard] = []
    for row in rows[:6]:
        metric = _clean_text(row.get("metric") or row.get("metric_id") or row.get("target_variable") or row.get("concept_id") or "Reconciliation")
        period = _clean_text(row.get("period") or row.get("fiscal_year") or row.get("as_of") or "")
        quality = _clean_text(row.get("trace_quality") or row.get("status") or row.get("severity") or "")
        formula = _clean_text(row.get("formula") or row.get("formula_with_values") or row.get("formula_trace") or row.get("message") or "")
        summary = " ".join(part for part in [metric, period, quality, formula] if part)
        if not summary:
            continue
        confidence: Confidence = "medium" if quality.lower() not in {"low", "fail", "failed"} else "low"
        source = EvidenceSource(
            kind="recon",
            source_id=stable_evidence_id("source", "recon", jurisdiction, entity, metric, period),
            label="Reconciliation flag",
            as_of=period or None,
            source_path="report_data_packet.recon_flags",
        )
        cards.append(
            EvidenceCard(
                card_id=stable_evidence_id("card", "recon", jurisdiction, entity, metric, period, quality),
                kind="recon",
                title=metric,
                summary=truncate_excerpt(summary, 260),
                excerpt=truncate_excerpt(summary),
                as_of=period or None,
                confidence=confidence,
                source=source,
                citations=[
                    EvidenceCitation(
                        citation_id=stable_evidence_id("cite", "recon", jurisdiction, entity, metric, period),
                        source_id=source.source_id,
                        label=quality or metric,
                        quote=truncate_excerpt(summary),
                    )
                ],
                tags=[ticker, "data-quality"],
            )
        )
    return cards


def _data_quality_evidence(
    ticker: str,
    jurisdiction: str,
    entity: str,
    report: dict[str, Any] | None,
) -> list[EvidenceCard]:
    if not isinstance(report, dict) or not report:
        return []
    findings = [row for row in (report.get("findings") or []) if isinstance(row, dict)]
    reconciliations = [row for row in (report.get("metric_reconciliations") or []) if isinstance(row, dict)]
    worst = sorted(
        findings,
        key=lambda row: (
            {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(_clean_text(row.get("severity")), 9),
            _clean_text(row.get("layer")),
        ),
    )[:5]
    bits = []
    for row in worst:
        bits.append(f"{row.get('layer')} {row.get('severity')}: {row.get('title')}")
    if reconciliations:
        bits.append(f"{len(reconciliations)} Yahoo/filing metric reconciliation(s)")
    if not bits:
        bits.append("No high-severity data-quality findings.")
    score = report.get("overall_score")
    summary = f"Data-quality score {score}/100. " if score is not None else ""
    summary += "; ".join(bits)
    as_of = _clean_text(report.get("as_of") or "")[:10] or None
    source = EvidenceSource(
        kind="data_quality",
        source_id=stable_evidence_id("source", "data_quality", jurisdiction, entity, as_of),
        label="Data-quality agent report",
        as_of=as_of,
        source_path="committee.data_quality_report",
    )
    citations = [
        EvidenceCitation(
            citation_id=stable_evidence_id("cite", "data_quality", jurisdiction, entity, row.get("finding_id")),
            source_id=source.source_id,
            label=_clean_text(row.get("finding_id") or row.get("title")),
            quote=truncate_excerpt(row.get("message") or row.get("title") or ""),
        )
        for row in worst
    ]
    return [
        EvidenceCard(
            card_id=stable_evidence_id("card", "data_quality", jurisdiction, entity, as_of, summary[:80]),
            kind="data_quality",
            title="Data-quality agent",
            summary=truncate_excerpt(summary, 260),
            excerpt=truncate_excerpt(summary),
            as_of=as_of,
            confidence="high" if score is not None else "medium",
            source=source,
            citations=citations,
            tags=[ticker, "data-quality", "audit"],
        )
    ]


def _runtime_context_cards(
    ticker: str,
    jurisdiction: str,
    entity: str,
    macro: dict[str, Any] | None,
    news: dict[str, Any] | None,
    ownership: dict[str, Any] | None,
) -> list[EvidenceCard]:
    cards: list[EvidenceCard] = []
    if isinstance(news, dict) and news:
        card = _news_card(ticker, jurisdiction, entity, news)
        if card:
            cards.append(card)
    if isinstance(macro, dict) and macro:
        card = _macro_card(ticker, jurisdiction, entity, macro)
        if card:
            cards.append(card)
    if isinstance(ownership, dict) and ownership:
        card = _ownership_card(ticker, jurisdiction, entity, ownership)
        if card:
            cards.append(card)
    return cards


def _news_card(ticker: str, jurisdiction: str, entity: str, news: dict[str, Any]) -> EvidenceCard | None:
    headlines = news.get("headlines") or news.get("items") or news.get("articles") or []
    headline_bits: list[str] = []
    headline_date = None
    for item in headlines[:4] if isinstance(headlines, list) else []:
        if isinstance(item, dict):
            headline_bits.append(_clean_text(item.get("title") or item.get("headline") or item.get("summary") or ""))
            headline_date = headline_date or item.get("published") or item.get("published_at") or item.get("date")
        else:
            headline_bits.append(_clean_text(item))
    sentiment = news.get("sentiment") or news.get("avg_sentiment") or news.get("score")
    summary_parts = []
    if sentiment not in (None, ""):
        summary_parts.append(f"Sentiment {sentiment}")
    summary_parts.extend(bit for bit in headline_bits if bit)
    summary = "; ".join(summary_parts) or truncate_excerpt(news.get("summary") or news, 260)
    if not summary:
        return None
    as_of = _clean_text(news.get("as_of") or news.get("updated_at") or news.get("date") or headline_date or "")[:10] or None
    source = EvidenceSource(
        kind="news",
        source_id=stable_evidence_id("source", "news", jurisdiction, entity, as_of, summary[:80]),
        label="News sentiment summary",
        as_of=as_of,
        source_path="news.sentiment_scores",
    )
    return EvidenceCard(
        card_id=stable_evidence_id("card", "news", jurisdiction, entity, as_of, summary[:80]),
        kind="news",
        title="News sentiment",
        summary=truncate_excerpt(summary, 260),
        excerpt=truncate_excerpt(summary),
        as_of=as_of,
        confidence="medium",
        source=source,
        citations=[
            EvidenceCitation(
                citation_id=stable_evidence_id("cite", "news", jurisdiction, entity, as_of, summary[:80]),
                source_id=source.source_id,
                label="News summary",
                quote=truncate_excerpt(summary),
            )
        ],
        tags=[ticker, "qualitative"],
    )


def _macro_card(ticker: str, jurisdiction: str, entity: str, macro: dict[str, Any]) -> EvidenceCard | None:
    regime = macro.get("regime") if isinstance(macro.get("regime"), dict) else {}
    signal = macro.get("signal") if isinstance(macro.get("signal"), dict) else {}
    fallback = "; ".join(
        part
        for part in [
            f"Regime {regime.get('quadrant')}" if regime.get("quadrant") else "",
            f"tilt {signal.get('tilt')}" if signal.get("tilt") else "",
            f"10Y {signal.get('ten_year')}" if signal.get("ten_year") is not None else "",
            f"2s10s {signal.get('yield_curve_2s10s')}" if signal.get("yield_curve_2s10s") is not None else "",
        ]
        if part
    )
    summary = truncate_excerpt(
        macro.get("summary")
        or macro.get("story")
        or fallback
        or macro,
        260,
    )
    if not summary:
        return None
    as_of = _clean_text(macro.get("as_of") or macro.get("date") or macro.get("updated_at") or regime.get("period_end") or "")[:10] or None
    source = EvidenceSource(
        kind="macro",
        source_id=stable_evidence_id("source", "macro", jurisdiction, entity, as_of, summary[:80]),
        label="Macro regime state",
        as_of=as_of,
        source_path="committee.macro",
    )
    return EvidenceCard(
        card_id=stable_evidence_id("card", "macro", jurisdiction, entity, as_of, summary[:80]),
        kind="macro",
        title="Macro regime",
        summary=summary,
        excerpt=summary,
        as_of=as_of,
        confidence="medium",
        source=source,
        citations=[
            EvidenceCitation(
                citation_id=stable_evidence_id("cite", "macro", jurisdiction, entity, as_of, summary[:80]),
                source_id=source.source_id,
                label="Macro state",
                quote=summary,
            )
        ],
        tags=[ticker, "macro"],
    )


def _ownership_card(
    ticker: str,
    jurisdiction: str,
    entity: str,
    ownership: dict[str, Any],
) -> EvidenceCard | None:
    direction = ownership.get("net_direction")
    passive_share = ownership.get("passive_share_of_reported_pct")
    holder_count = ownership.get("holder_count")
    quarter = ownership.get("quarter")
    fallback = "; ".join(
        part
        for part in [
            f"Quarter {quarter}" if quarter else "",
            f"net {direction}" if direction else "",
            f"passive share {passive_share}%" if passive_share is not None else "",
            f"{holder_count} reported holders" if holder_count is not None else "",
        ]
        if part
    )
    summary = truncate_excerpt(
        ownership.get("summary")
        or ownership.get("institutional_summary")
        or fallback
        or ownership,
        260,
    )
    if not summary:
        return None
    as_of = _clean_text(ownership.get("as_of") or ownership.get("quarter") or ownership.get("date") or "")[:10] or None
    source = EvidenceSource(
        kind="ownership",
        source_id=stable_evidence_id("source", "ownership", jurisdiction, entity, as_of, summary[:80]),
        label="Ownership state",
        as_of=as_of,
        source_path="committee.ownership",
    )
    return EvidenceCard(
        card_id=stable_evidence_id("card", "ownership", jurisdiction, entity, as_of, summary[:80]),
        kind="ownership",
        title="Ownership",
        summary=summary,
        excerpt=summary,
        as_of=as_of,
        confidence="high",
        source=source,
        citations=[
            EvidenceCitation(
                citation_id=stable_evidence_id("cite", "ownership", jurisdiction, entity, as_of, summary[:80]),
                source_id=source.source_id,
                label="Ownership summary",
                quote=summary,
            )
        ],
        tags=[ticker, "ownership"],
    )


def _relation_exists(name: str) -> bool:
    try:
        df = read_sql("SELECT to_regclass(%(name)s)::text AS relation_name", {"name": name})
    except Exception:
        return False
    rows = _records(df)
    return bool(rows and rows[0].get("relation_name"))


def _packet_rows(packet: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = packet.get(key)
    if isinstance(section, dict):
        rows = section.get("rows") or section.get("data") or section.get("items")
    else:
        rows = section
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if hasattr(df, "to_dict"):
        return df.to_dict(orient="records")
    if isinstance(df, list):
        return [row for row in df if isinstance(row, dict)]
    return []


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = str(value)
    text = str(value)
    return re.sub(r"\s+", " ", text).strip()
