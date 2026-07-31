"""Live end-to-end verification for the committee DQ / mapping agent.

Applies the dq_finding_state migration, runs the DQ node for one ticker with the
review-queue write ENABLED, optionally promotes the top mapping proposal, and prints the
review-queue / governed-table / finding-state contents for inspection.

Run from backend/ with a DeepSeek key in the environment:

    python ai_analyst/scripts/dq_agent_live_verify.py                      # AIG, write queue only
    python ai_analyst/scripts/dq_agent_live_verify.py --promote            # + promote top proposal
    python ai_analyst/scripts/dq_agent_live_verify.py MET 0000064996 insurance --promote

WRITES PRODUCTION DATA: the review queue always; the governed mapping table with --promote.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from xbrl_sec.sec.db.connection import connect
from xbrl_sec.llm.chat_deepseek import resolve_env_key
from ai_analyst._db import read_sql
from ai_analyst import mapping_promote
from ai_analyst.committee import nodes

_MIGRATION = "xbrl_sec/sec/sql/131_dq_finding_state.sql"


def _apply_migration() -> None:
    sql = Path(_MIGRATION).read_text(encoding="utf-8-sig")
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
    print(f"migration applied: {_MIGRATION}")


def _run_node(ticker: str, cik: str, sector: str) -> dict:
    state = {
        "ticker": ticker,
        "jurisdiction": "US",
        "cik": cik,
        "api_key": resolve_env_key(),
        "data_quality_report": {
            "findings": [
                {
                    "finding_id": "dq-inv-1",
                    "layer": "standardized",
                    "severity": "high",
                    "title": "Investment-portfolio concepts unmapped",
                    "message": "AFS/HTM/fixed-maturity present in raw facts, absent from investment_securities.",
                }
            ],
            "layer_scores": {"raw": 90, "standardized": 68, "metrics": 80, "recon": 75},
        },
        "financial_ratios": {"modeled_statements": {"sector_scope": sector}, "company": {"name": ticker}},
        "config": {"dq_agent_always": True, "dq_agent_queue_proposals": True},
    }
    out = nodes.data_quality_agent_node(state)["data_quality_agent"]
    print(f"\nqueued_proposal_ids: {out.get('queued_proposal_ids')}")
    print(f"triage_skipped_reason: {out.get('triage_skipped_reason')}")
    print(f"finding_deltas: {out.get('finding_deltas')}")
    return out


def _print_queue() -> None:
    q = read_sql(
        """
        SELECT normalized_concept_id, mapping_sector, review_class, top_candidate_label,
               proposed_action, confidence, review_status, review_batch
        FROM map_concept_to_taxonomy_review_queue
        WHERE mapping_source = 'committee_dq_agent_v1'
        ORDER BY review_status, confidence DESC NULLS LAST
        """
    )
    print(f"\n=== review-queue rows (committee_dq_agent_v1): {len(q)} ===")
    print(q.to_string(index=False) if not q.empty else "(none)")


def _promote_top(out: dict) -> None:
    proposals = ((out.get("triage") or {}).get("proposals")) or []
    candidates = [
        p for p in proposals
        if str(p.get("kind") or "").startswith("mapping") and p.get("concept_id") and p.get("target_variable")
    ]
    candidates.sort(key=lambda p: float(p.get("confidence") or 0), reverse=True)
    if not candidates:
        print("\n(no mapping proposal to promote)")
        return
    top = candidates[0]
    print(f"\npromoting: {top['concept_id']} -> {top['target_variable']} (sector={top.get('mapping_sector')})")
    result = mapping_promote.promote_proposal(
        jurisdiction="US",
        concept_id=top["concept_id"],
        mapping_sector=top.get("mapping_sector"),
        target_variable=top.get("target_variable"),
    )
    print(f"promote result: {result}")


def _governance_check() -> None:
    v = read_sql(
        """
        SELECT mapping_source, COUNT(*) AS n
        FROM map_concept_to_taxonomy_versioned
        WHERE mapping_source IN ('committee_dq_agent_v1', 'committee_dq_agent_promotion')
        GROUP BY mapping_source
        """
    )
    print("\n=== governed table rows from the committee agent ===")
    print(v.to_string(index=False) if not v.empty else "(none — nothing promoted)")
    print("  (committee_dq_agent_v1 must be 0: the node never writes production; "
          "committee_dq_agent_promotion appears only after an explicit Promote)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", default="AIG")
    parser.add_argument("cik", nargs="?", default="0000005272")
    parser.add_argument("sector", nargs="?", default="insurance")
    parser.add_argument("--promote", action="store_true", help="Promote the top mapping proposal to production.")
    parser.add_argument("--skip-migration", action="store_true")
    args = parser.parse_args()

    if not args.skip_migration:
        _apply_migration()
    out = _run_node(args.ticker, args.cik, args.sector)
    _print_queue()
    if args.promote:
        _promote_top(out)
        _print_queue()
    _governance_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
