# Investment Committee (multi-agent LangGraph)

A self-contained LangGraph pipeline that turns the deterministic `ai_analyst`
data layer into an institutional equity-research memo. It layers a dialectical
tribunal (Advocate / Challenger / Auditor plus sector-aware specialist archetypes
→ Lead synthesizer) plus a rigorous, auditable valuation engine on top of the
existing `services` / `dcf_engine`.

Everything — code and outputs — lives inside this repo. Outputs are written to
`<repo>/output/committee/<date>/<TICKER>/` (git-ignored).

## Run it

```powershell
# Full run (deepseek-reasoner analysis + deepseek-chat structured extraction):
python -m ai_analyst.committee.run --ticker MSFT --years 2022 2023 2024 2025

# Token-free deterministic smoke test:
python -m ai_analyst.committee.run --ticker MSFT --offline --no-news

# Graph only (no report render), prints a summary:
python -m ai_analyst.committee.graph --ticker MSFT
```

Also exposed over HTTP: `POST /api/ai/committee` (`api/routers/ai_committee.py`).

## Topology

```
gate: completeness → dq_validation ─(fail)→ error_terminator
   └─(pass)→ engine (fundamentals · segments · WACC · comps · cash-flow/ROIC · reverse-DCF)
        ├→ news_macro ┐
        └→ institutional ┴→ {advocate, challenger, auditor, specialists} → lead → memo
                                     (macro-adjusted weights; capped review loop)
```

## Modules

| File | Role |
|---|---|
| `graph.py` | LangGraph assembly, routers, `run_committee()`, CLI |
| `archetypes.py` | Built-in specialist analyst registry + sector-priority deployment |
| `run.py` | End-to-end CLI: (news) → committee → branded PDF, all into `<repo>/output` |
| `state.py` | `InvestmentCommitteeState` + `CommitteeConfig` (models, weights, toggles) |
| `nodes.py` | All node functions + two-tier LLM helpers (reasoner / structured) |
| `prompts.py` | Advocate/Challenger/Auditor/Lead/Memo prompts (reasoner-oriented) |
| `schemas.py` | Pydantic structured-output schemas |
| `wacc.py` | Fama-French / CAPM WACC with credit spread + audit trail |
| `valuation.py` | Consolidated DCF, growth×WACC grid, **SOTP (primary)**, reverse-DCF, triangulation |
| `comps.py` | 10 largest GICS peers by market cap (EV/EBITDA, EV/EBIT, EV/FCF, P/E, PEG, implied) |
| `marketdata.py` | Prices + multi-year cash-flow/capex history + incremental ROIC |
| `segments.py` | Off-statement segment extraction (iXBRL) + multi-year trend |
| `institutional.py` | 13F ownership (top holders, QoQ, active/passive) |
| `newsmacro.py` | Macro regime/rates + news sentiment (macro-led fallback) + ingestion helper |
| `charts.py` | Inline SVG charts (football-field, ROIC-vs-WACC, capex, price, trend, ownership, heatmap) |
| `report_pdf.py` | Story-first + appendix HTML → PDF (headless Chrome), MZQA design tokens |

## Models

Two-tier and multi-provider. The provider comes from `state["provider"]`
(registry: `backend/llm_providers.py` — DeepSeek, OpenAI, Anthropic, Moonshot,
Gemini) and both tiers resolve against it via `xbrl_sec.llm.factory`:

- **reasoning tier** — narrative/analysis in plain text (`deepseek-reasoner`;
  `claude-opus-4-8` with adaptive thinking on Anthropic). DeepSeek's reasoner is a
  thinking-mode model without reliable tool output, which is why extraction does
  not run on it.
- **structured tier** — scenario/weight extraction via `with_structured_output`
  (`deepseek-chat` and equivalents).

`CommitteeConfig.reasoning_model`/`structured_model` still pin a model explicitly;
their legacy DeepSeek defaults are treated as "unset" when another provider is
selected (see `nodes._LEGACY_DEEPSEEK_MODELS`). Specialist analysts also emit
optional structured `SpecialistVerdict` objects for sensitivity and
peer-comparison signals.
