"""End-to-end committee runner (repo-resident CLI).

One command drives the whole workflow and writes every output inside the repo
(``<repo>/output/committee/<date>/<TICKER>/``):

    python -m ai_analyst.committee.run --ticker MSFT --years 2022 2023 2024 2025

Steps: (optional) news ingestion → committee LangGraph (deepseek-reasoner for
analysis/narrative, deepseek-chat for structured extraction) → branded story +
appendix HTML/PDF via headless Chrome. Use ``--offline`` for a token-free
deterministic smoke test.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from . import graph, newsmacro, report_pdf


def _load_env() -> None:
    """Best-effort load of the repo-root .env so the CLI is turnkey."""
    root = Path(__file__).resolve().parents[3]
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def run(ticker: str, years: list[int], *, news: bool = True, report: bool = True,
        offline: bool = False, provider: str | None = None,
        reasoning_model: str = "", structured_model: str = "") -> dict:
    """Empty model names mean 'use the provider's registry default'."""
    _load_env()
    if offline:
        os.environ["MZQA_COMMITTEE_DISABLE_LLM"] = "1"

    if news and not offline:
        try:
            t = time.time()
            res = newsmacro.ensure_news(ticker, limit=15)
            print(f"[news] {time.time()-t:.0f}s: {json.dumps(res, default=str)[:200]}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[news] skipped: {exc}", flush=True)

    cfg = {"reasoning_model": reasoning_model, "structured_model": structured_model,
           "scenario_weight_mode": "macro_adjusted", "enable_news": news}
    t = time.time()
    state = graph.run_committee(ticker, years, config=cfg, provider=provider)
    print(f"[committee] {time.time()-t:.1f}s", flush=True)
    print(graph._summary(state), flush=True)

    out_dir = report_pdf.report_dir_for(ticker)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "final_state.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    if report:
        r = report_pdf.write_report(state, out_dir)
        print(f"[report] {r.get('pdf') or r.get('html')}", flush=True)
    return state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the MZQA investment committee end-to-end.")
    p.add_argument("--ticker", required=True)
    p.add_argument("--years", type=int, nargs="*", default=[2022, 2023, 2024, 2025])
    p.add_argument("--no-news", action="store_true", help="Skip news ingestion (macro-only).")
    p.add_argument("--no-report", action="store_true", help="Skip the PDF render.")
    p.add_argument("--offline", action="store_true", help="Deterministic, no LLM (token-free smoke test).")
    p.add_argument("--provider", default=None,
                   help="LLM provider: deepseek | openai | anthropic | moonshot | gemini.")
    p.add_argument("--reasoning-model", default="", help="Default: the provider's reasoning model.")
    p.add_argument("--structured-model", default="", help="Default: the provider's chat model.")
    args = p.parse_args(argv)
    run(args.ticker, args.years, news=not args.no_news, report=not args.no_report,
        offline=args.offline, provider=args.provider,
        reasoning_model=args.reasoning_model, structured_model=args.structured_model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
