"""Regenerate ``docs/committee_member_prompts.md`` from the live committee prompts.

The reference doc is a verbatim capture of every committee analyst's LLM prompt. Rather than
hand-maintain it (and let it drift from ``prompts.py`` / ``archetypes.py``), this script renders the
persona + shared ground-rules sections directly from code:

  - Sections 1-3  (Advocate / Challenger / Auditor)  <- ``prompts.{ADVOCATE,CHALLENGER,AUDITOR}_PROMPT``
  - Sections 4-9  (the six specialist archetypes)     <- ``nodes._extra_persona_prompt`` over
                                                         ``archetypes._roster_entry(...)``
  - Appendix B    (shared closing instruction)         <- the line ``_run_agent`` appends to each prompt
  - Appendix A    (the illustrative EVIDENCE JSON) is produced only by a live run, so it is PRESERVED
                  verbatim from the existing doc (spliced back unchanged on every regeneration).

Run (from ``backend/``):

    python -m ai_analyst.committee.scripts.dump_committee_prompts

Pass ``--check`` to exit non-zero if the doc is stale (for CI) instead of rewriting it.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script (python path/to/dump_committee_prompts.py), not just `-m`.
_BACKEND = Path(__file__).resolve().parents[3]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from ai_analyst.committee import archetypes, nodes, prompts  # noqa: E402

DOC_PATH = _BACKEND.parent / "docs" / "committee_member_prompts.md"

# Representative company so the specialists' "Company context:" line renders deterministically.
_SAMPLE_COMPANY = {"gics_sector_name": "Information Technology", "gics_industry_name": "Software"}

# Editorial one-line subtitles for the three core stances (specialists all say "specialist").
_CORE = [
    ("The Advocate", "builds the bull case", prompts.ADVOCATE_PROMPT),
    ("The Challenger", "the constructive skeptic", prompts.CHALLENGER_PROMPT),
    ("The Auditor", "cold, quantitative adjudicator", prompts.AUDITOR_PROMPT),
]

# Stable specialist order (registry insertion order), independent of per-run sector priority.
_SPECIALIST_ORDER = list(archetypes.SPECIALIST_ANALYSTS)

_HEADER = """# Committee member prompts

> **Generated file — do not edit by hand.** Regenerate with
> `python -m ai_analyst.committee.scripts.dump_committee_prompts` (from `backend/`).

The exact prompt each committee analyst submits to the LLM. The nine analysts each make one narrative
call (`_reason` in [`backend/ai_analyst/committee/nodes.py`](../backend/ai_analyst/committee/nodes.py)).
(A tenth, downstream call — the Lead Analyst's memo — is a synthesis *over* these nine outputs rather
than an analyst's own analysis, so it is not reproduced here.)

Each analyst prompt is assembled as three parts:

```
<persona / system prompt>          # unique per analyst — sections 1-9 below
EVIDENCE (JSON — …): <evidence>     # identical for all analysts — Appendix A
Write the <STANCE> case now, …      # shared instruction — Appendix B
```

The persona sections and shared ground rules below are rendered verbatim from `prompts.py` and
`archetypes.py`. The specialists' `Company context:` line is shown for a representative Information
Technology / Software company (it varies per run). Appendix A is an illustrative evidence packet
captured from a live **MSFT** run (US market) and is preserved across regenerations.

---"""

_APPENDIX_B = """## Appendix B — shared closing instruction

Appended (identically, bar the one-word stance) to every analyst prompt above. Mirrors the closing
line `_run_agent` appends in [`nodes.py`](../backend/ai_analyst/committee/nodes.py).

```text
Write the <STANCE> case now, following the output format above. Be quantitative and cite the numbers from the evidence. Plain text, no JSON.
```"""

_APPENDIX_A_PLACEHOLDER = """## Appendix A — shared EVIDENCE block

_(placeholder — run the committee once and paste the live `EVIDENCE (JSON …)` packet here inside a
```text fence; this block is preserved verbatim on future regenerations.)_"""


def _section(n: int, title: str, subtitle: str, body: str) -> str:
    return f"## {n}. {title}\n\n*{subtitle}*\n\n```text\n{body.strip()}\n```"


def _specialist_persona(key: str) -> tuple[str, str]:
    entry = archetypes._roster_entry(key, _SAMPLE_COMPANY)
    return entry["name"], nodes._extra_persona_prompt(entry["name"], entry["mandate"])


def _extract_appendix_a(existing: str) -> str:
    """Preserve the illustrative Appendix A block verbatim from the current doc."""
    start = existing.find("## Appendix A")
    if start == -1:
        return _APPENDIX_A_PLACEHOLDER
    end = existing.find("## Appendix B", start)
    block = existing[start:end] if end != -1 else existing[start:]
    return block.strip()


def render(existing_doc: str) -> str:
    blocks: list[str] = [_HEADER.strip()]
    n = 1
    for title, subtitle, body in _CORE:
        blocks.append(_section(n, title, subtitle, body))
        n += 1
    for key in _SPECIALIST_ORDER:
        name, body = _specialist_persona(key)
        blocks.append(_section(n, name, "specialist", body))
        n += 1
    blocks.append("---")
    blocks.append(_extract_appendix_a(existing_doc))
    blocks.append(_APPENDIX_B.strip())
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
    new_doc = render(existing)
    if check:
        if existing != new_doc:
            print(f"STALE: {DOC_PATH} is out of date. Run without --check to regenerate.")
            return 1
        print(f"OK: {DOC_PATH} is up to date.")
        return 0
    # Write LF regardless of platform so the checked-in doc stays newline-clean.
    with open(DOC_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_doc)
    print(f"Wrote {DOC_PATH} ({len(new_doc)} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
