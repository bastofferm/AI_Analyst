# Per-Ticker DQ & Mapping-Check Agent — Concept and Gap Analysis

Date: 2026-07-07
Status: proposal (builds on the deterministic DQ agent and insurance sector mapping work landed 2026-07-05 … 07-07)

## 1. What Codex recently landed (reviewed state)

### 1.1 Insurance sector mapping pipeline (xbrl_sec)

| Change | File | Effect |
|---|---|---|
| Sector-override mappings | `backend/xbrl_sec/sec/sql/130_insurance_investment_mapping_reconciliation.sql` | Clones `non_bank_financial` mappings whose `target_variable='short_term_investments'` and whose concept looks like an investment-portfolio fact (AFS / HTM / FixedMaturity / SummaryOfInvestments / …) into `mapping_sector='insurance'` rows targeting `investment_securities`. Stops insurers' operating investment portfolios from being subtracted as "excess cash" in corporate net debt. |
| Sector hierarchy split | `backend/xbrl_sec/sec/std/us_standardize.py`, `jp_standardize.py` (`_hierarchy_sector`) | `non_bank_financial` is resolved into `reit` (GICS 60), `insurance` (industry group 4030), else `asset_manager_other_financial`. |
| Insurance net-debt derivation | `backend/xbrl_sec/sec/std/graph_closure.py` (+ `tests/test_graph_closure.py`) | For `sector_scope == "insurance"` the derived `net_debt` formula becomes `total_financial_debt - cash_and_cash_equivalents` (no short-term-investment offset). |
| Sector-aware committee expectations | `backend/ai_analyst/services.py` | `INSURANCE_LINE_ITEMS`, `sector_scope_from_company()` insurance branch, `line_items_for_sector()`, `peer_metric_ids_for_sector()`, `yfinance_cross_check_items_for_sector()`. |

### 1.2 Deterministic data-quality agent (ai_analyst, new 07-07)

- `backend/ai_analyst/data_quality_agent.py` — rule-based, read-only report over exactly the five layers: **raw** (`source_filing_state`, `fact_fundamentals_*`), **standardized** (packet rows vs sector-expected line items + identity breaks), **metrics** (sector-expected derived metrics), **recon** (`fact_metrics_recon_*` trace quality), **yahoo_cross_check**. Produces `DataQualityAgentReport` (layer scores 0–100, severity-ranked `DataQualityFinding`s with stable IDs, `MetricReconciliation`s with raw trace, canned `repair_suggestions`). Sector-aware via `services.line_items_for_sector`.
- Wired inline into `financial_analysis_engine_node` (`committee/nodes.py`), stored as `state["data_quality_report"]`, folded into the evidence bundle (`evidence.py::_data_quality_evidence`) and into tribunal/memo prompts as `data_quality_report_compact` (`prompts.py` tells analysts to cite finding IDs).
- Surfaced through `POST /committee` (`backend/api/routers/ai_committee.py`) and rendered read-only in `frontend/src/components/committee/DataQualityAgentPanel.tsx`.
- Tests green: `backend/ai_analyst/tests/test_data_quality_agent.py`, `test_llm_runtime_json.py` (run from `backend/`).

### 1.3 Relevant pre-existing machinery

- **DeepSeek-only LLM runtime** `backend/ai_analyst/llm_runtime.py`: `chat_json` (forced JSON + repair parsing), `chat_with_tools` (multi-hop tool loop with DSML fallback). Per-request API key.
- **Mapping governance** (xbrl_sec): governed table `map_concept_to_taxonomy_versioned`; advisory `map_concept_to_taxonomy_review_queue`; `sec/graphs/concept_mapping.py` = existing DeepSeek agentic loop that proposes `ConceptMappingProposal`s (auto-promote ≥ 0.85 confidence, else review queue); `sources/mapping_health.py` (never mutates the governed table); scripts `audit_sector_mapping_gaps.py`, `seed_sector_mapping_queue.py`, `promote_sector_mapping_queue.py` (dry-run promote).

## 2. Gap analysis — what is missing for the requested feature

The request: a per-ticker DQ/mapping check inside single-stock analysis, run by a **separate DeepSeek agent** that **immediately proposes changes and a way forward** for issues in raw / standardized / metrics / recon data, as a **LangGraph node**.

| # | Gap | Detail |
|---|---|---|
| G1 | Not a graph node | The DQ report is computed inline inside `financial_analysis_engine_node`; there is no dedicated node, so it cannot be routed, retried, toggled, or timed independently. |
| G2 | No LLM reasoning | The agent is purely deterministic. `repair_suggestions` are canned strings; no root-cause triage, no confidence, no proposal objects. DeepSeek is not involved. |
| G3 | No per-ticker mapping check | The DQ agent flags *missing line items* but never inspects **which raw concepts for this entity went unmapped or were routed to the wrong target/sector**. The mapping-gap tooling (audit/seed scripts, concept-mapping agent) runs corpus-wide in the ingest pipeline, not per ticker, and is not triggered by committee findings. |
| G4 | No proposal channel | Nothing bridges committee DQ findings to `map_concept_to_taxonomy_review_queue`. "Immediately proposes changes" currently means a text bullet in the UI. |
| G5 | No finding lifecycle | `FindingStatus` (open/explained/resolved) exists on the model but nothing persists findings; every run starts from zero, no delta vs previous run. |
| G6 | Read-only frontend | `DataQualityAgentPanel` displays findings; there is no proposals section, no queue status, no way-forward checklist. |

## 3. Concept: `data_quality_agent_node` (DeepSeek) in the committee graph

### 3.1 Topology

```
completeness_check → dq_validation ─(fail & strict)→ error_terminator
     └─(pass)→ financial_analysis_engine
                   ├→ news_macro ──────────┐
                   ├→ institutional ───────┼→ {bull, bear, auditor, specialists} → lead → memo
                   └→ data_quality_agent ──┘        (tribunal waits on all three)
```

- Move `build_data_quality_report(...)` **out of** the engine node into the new `data_quality_agent_node`.
- The node runs in parallel with `news_macro` / `institutional` (it needs the packet, so it sits after the engine; it adds no critical-path latency beyond the slowest parallel branch).
- Tribunal keeps consuming `data_quality_report_compact`, now enriched with LLM triage.

### 3.2 Node pipeline (inside the node)

1. **Deterministic scan** — existing `build_data_quality_report()` unchanged (fast, reproducible, zero tokens).
2. **Per-ticker mapping evidence pack** (new, deterministic SQL; the core of the "mapping check"):
   - *Unmapped concepts*: for this entity's latest filings, raw facts with no row in `map_concept_to_taxonomy_versioned` (jurisdiction + sector compatible, year in effective range) — top N by absolute value, with concept label, statement location, value, unit.
   - *Sector-compatibility check*: entity-scoped version of `audit_sector_mapping_gaps.py` — for the entity's `sector_scope`, expected display-profile line items whose feeding mappings only exist under an incompatible `mapping_sector` (this is exactly the class of bug the insurance `short_term_investments` → `investment_securities` migration fixed; the check makes the next one visible per ticker instead of via a global audit).
   - *Suspect mappings behind broken metrics*: for recon rows with bad/missing trace, resolve `source_concept_ids` → current mapping rows (target, sector, sign_policy, multiplier).
3. **Escalation gate** (cost control): call DeepSeek only if (a) any finding ≥ `medium`, or (b) any layer score < 85, or (c) the mapping pack is non-empty, or (d) `config["dq_agent_always"]`. `MZQA_COMMITTEE_DISABLE_LLM=1` (offline mode) always skips the LLM.
4. **DeepSeek triage call** — `llm_runtime.chat_json` with the `structured_model` (default `deepseek-chat`; optional escalation to `deepseek-reasoner` when a `blocker` is present). Input: compact findings + reconciliations + mapping evidence pack + sector scope + expected line items. Output schema (Pydantic-validated):

```jsonc
{
  "triage": [
    { "finding_id": "dq-…", "root_cause": "mapping_gap | wrong_target_variable | sector_scope_mismatch |
        sign_or_multiplier | parse_gap | period_mismatch | currency_or_unit | formula_gap |
        source_data_gap | benign_definition_difference",
      "explanation": "…", "priority": 1 }
  ],
  "proposals": [
    { "kind": "mapping_add | mapping_retarget | mapping_sector_override | reparse_filing |
        restandardize_entity | recompute_metrics | refresh_yahoo | no_action",
      "concept_id": "us-gaap:…",          // mapping kinds only
      "target_variable": "…",
      "mapping_sector": "insurance",
      "confidence": 0.0-1.0,
      "reasoning": "…",
      "evidence_finding_ids": ["dq-…"],
      "next_step": "exact CLI command or queue action" }
  ],
  "way_forward": ["ordered remediation steps, most impactful first"],
  "narrative": "2–3 sentences for the committee"
}
```

5. **Proposal routing** (governance boundary — same philosophy as `mapping_health.py`):
   - Mapping proposals are **written to `map_concept_to_taxonomy_review_queue`** with `mapping_source='committee_dq_agent_v1'`, `review_status='queued'`, evidence JSON = finding IDs + values + trace. **Never** insert into `map_concept_to_taxonomy_versioned` from a committee run, regardless of confidence — promotion stays with the existing review/promote path (`promote_sector_mapping_queue.py` / review UI). Dedupe on (concept_id, target_variable, mapping_sector, jurisdiction) against open queue rows.
   - Pipeline proposals (reparse / restandardize / recompute) surface as `way_forward` entries with exact commands (the deterministic layer already synthesizes `fundamentals.run …` strings).
6. **Failure isolation**: any LLM/queue error degrades to the deterministic report plus a warning (`"DQ LLM triage unavailable: …"`); the node never fails the run.

### 3.3 State, config, prompts, API, frontend

- **State** (`committee/state.py`): keep `data_quality_report`; add `llm_triage`, `proposals`, `way_forward`, `narrative`, `queued_proposal_ids`, `llm_model`, `skipped_reason` nested under a new `data_quality_agent` key (or extend the report dict — prefer nesting to keep the deterministic report schema stable).
- **Config** (`default_config()`): `dq_agent_enable: True`, `dq_agent_always: False`, `dq_agent_queue_proposals: True`, `dq_agent_model: None` (falls back to `structured_model`).
- **Prompts** (`compact_data_quality_report`): append `narrative` and top-3 proposals so bull/bear/auditor/lead see the triage, with the existing instruction to cite finding IDs and never invent repaired numbers.
- **API** (`ai_committee.py`): add `data_quality_agent: dict | None` to `CommitteeResponse` (report already flows). Optional later: `POST /committee/dq/queue-proposal` for manually accepting a proposal the agent didn't auto-queue.
- **Frontend** (`DataQualityAgentPanel.tsx`): two new sections — *Proposals* (kind badge, concept → target, confidence, "queued" pill) and *Way forward* (ordered checklist); narrative line under the header. Types in `lib/api.ts`.

### 3.4 Persistence (phase 2, optional)

`dq_finding_state` table keyed by the stable `finding_id` (already deterministic via `stable_dq_id`) with status open/explained/resolved, first_seen/last_seen run timestamps. Enables run-over-run deltas ("2 new, 1 resolved since last run") and lets the LLM mark benign definition differences as `explained` so they stop ranking above real issues.

## 4. Implementation checklist

1. `backend/ai_analyst/dq_triage.py` (new): Pydantic schemas (`DqTriageItem`, `DqProposal`, `DqAgentResult`), evidence-pack SQL (unmapped concepts, entity-scoped sector-compatibility, suspect mappings), prompt builder, `run_llm_triage(report, pack, *, api_key, model) -> DqAgentResult` via `chat_json`, review-queue writer with dedupe.
2. `committee/nodes.py`: new `data_quality_agent_node`; remove the inline `build_data_quality_report` call from `financial_analysis_engine_node`.
3. `committee/graph.py`: add node + edges (`engine → data_quality_agent`; `data_quality_agent → each tribunal agent`); bump recursion-limit comment.
4. `committee/state.py`: state field + config defaults. `committee/prompts.py` + `data_quality_agent.compact_data_quality_report`: include triage narrative/proposals.
5. `api/routers/ai_committee.py`: response field. `frontend`: `api.ts` types + `DataQualityAgentPanel` sections.
6. Tests: mocked `chat_json` happy path, malformed-JSON fallback, escalation gate (no findings → no LLM call), queue-writer dedupe + "no versioned-table writes" guard, offline mode.
7. Phase 2: `dq_finding_state` migration + delta logic; manual queue endpoint; `deepseek-reasoner` escalation for blockers.

## 5. Design decisions (and why)

- **Deterministic scan stays primary; LLM only triages.** Reproducible findings, tokens spent only on interpretation — mirrors the sentiment/mapping agents elsewhere in the repo.
- **Review queue, never direct mapping writes.** A committee run is a read path; the ingest pipeline's concept-mapping agent may auto-promote at ≥ 0.85 because it runs under pipeline governance, but a per-ticker analysis must not mutate corpus-wide mappings that affect every other ticker. Queue + existing promote flow keeps one approval chokepoint.
- **Single-shot `chat_json` in v1, not a tool loop.** The evidence pack is fully computable up front; a multi-hop `chat_with_tools` variant (reusing `concept_mapping.py`-style read-only tools) is a clean v2 if triage quality needs it.
- **Parallel branch, not a gate.** DQ findings are advisory context for the tribunal (consistent with `dq_enforce=False` default); blocking the run on LLM triage would couple report latency/cost to data-quality noise.
