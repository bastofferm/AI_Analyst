"""Nightly macro brief generator — bilingual EN/DE.

Two pipelines, one CLI:
  * generate_tile_captions(date) — one-line caption per tile slot
  * generate_macro_essay(date, session) — ~150-word daily brief

Both call DeepSeek once for both languages and persist to sec.fact_macro_story.
Skips regeneration when input_hash matches the prior run.

Run:
    python -m ai_analyst.macro_brief --captions --essay
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import sys
from typing import Any

import llm_providers
from xbrl_sec.sec.db.connection import connect

from ai_analyst.llm_runtime import (
    LLMError,
    chat_once,
    resolve_env_key,
)


def _robust_json(text: str) -> dict:
    """Tolerant JSON extractor: strips fences and isolates the first {..} block."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # try to grab the largest {...} balanced span
    start = s.find("{")
    if start < 0:
        raise LLMError(f"No JSON object in response: {s[:200]!r}")
    depth = 0
    end = -1
    for i, ch in enumerate(s[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end <= start:
        raise LLMError(f"Unbalanced braces in response: {s[:200]!r}")
    return json.loads(s[start:end])


def _chat_json_tolerant(*, api_key: str, system_prompt: str, user_prompt: str, max_tokens: int = 4000,
                        temperature: float = 0.2, provider: str | None = None) -> dict:
    msg = chat_once(
        api_key=api_key, provider=provider, model=llm_providers.chat_model(provider),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=temperature, max_tokens=max_tokens,
    )
    return _robust_json(msg.get("content") or "{}")
from ai_analyst.prompts import MACRO_ESSAY_PROMPT, MACRO_TILE_CAPTION_PROMPT

logger = logging.getLogger("mzqa.macro_brief")
PROMPT_VERSION = "macro_brief.v1"

# Kill switch — flip to True (or unset env var MZQA_MACRO_BRIEF_DISABLED) to re-enable.
# Set to False here AND honour env override so cron / scheduler runs no-op too.
import os as _os
MACRO_BRIEF_DISABLED = _os.environ.get("MZQA_MACRO_BRIEF_DISABLED", "1") != "0"


# ---------------------------------------------------------------------------
# Data packet assembly
# ---------------------------------------------------------------------------

def _fetch_tiles_for_caption(limit_per_juris: int = 14) -> list[dict[str, Any]]:
    """Return a compact list of tile inputs for the caption generator."""
    sql = """
        WITH targets AS (
            SELECT s.series_id, s.story_tile_slot AS slot, s.name AS label,
                   s.category, s.units, s.jurisdiction, s.importance,
                   row_number() OVER (PARTITION BY s.jurisdiction ORDER BY s.importance) AS rn
            FROM   ref_macro_series s
            WHERE  s.is_active = TRUE AND s.story_tile_slot IS NOT NULL
        ),
        latest AS (
            SELECT DISTINCT ON (f.series_id) f.series_id, f.date, f.value
            FROM   fact_macro f
            JOIN   targets t ON t.series_id = f.series_id
            ORDER  BY f.series_id, f.date DESC
        ),
        prev AS (
            SELECT DISTINCT ON (f.series_id) f.series_id, f.value AS prev_value
            FROM   fact_macro f
            JOIN   latest l ON l.series_id = f.series_id
            WHERE  f.date <= l.date - INTERVAL '1 year'
            ORDER  BY f.series_id, f.date DESC
        )
        SELECT t.slot, t.label, t.category, t.units, t.jurisdiction,
               l.date::text, l.value, p.prev_value
        FROM   targets t
        LEFT   JOIN latest l ON l.series_id = t.series_id
        LEFT   JOIN prev   p ON p.series_id = t.series_id
        WHERE  t.rn <= %s
        ORDER  BY t.jurisdiction, t.importance, t.slot
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (limit_per_juris,))
        rows = cur.fetchall()
    tiles: list[dict[str, Any]] = []
    for slot, label, cat, units, juris, asof, value, prev in rows:
        if value is None:
            continue
        change_yoy = None
        if prev is not None and prev != 0:
            change_yoy = round((float(value) - float(prev)) / abs(float(prev)) * 100, 2)
        tiles.append({
            "slot": slot, "label": label, "category": cat, "unit": units,
            "jurisdiction": juris, "as_of": asof,
            "value": round(float(value), 4),
            "prev": round(float(prev), 4) if prev is not None else None,
            "change_yoy_pct": change_yoy,
        })
    return tiles


def _fetch_essay_packet() -> dict[str, Any]:
    """Compact packet for the essay prompt."""
    tiles = _fetch_tiles_for_caption(limit_per_juris=8)
    by_j: dict[str, list[dict]] = {}
    for t in tiles:
        by_j.setdefault(t["jurisdiction"], []).append(t)

    # Curve summary
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.story_tile_slot, l.value
            FROM   ref_macro_series s
            LEFT   JOIN LATERAL (
                      SELECT value FROM fact_macro WHERE series_id=s.series_id ORDER BY date DESC LIMIT 1
                   ) l ON TRUE
            WHERE  s.story_tile_slot IN ('us_2y_yield','us_10y_yield','jp_call_rate','jp_10y_yield','us_policy_rate','jp_policy_rate')
        """)
        curve = {r[0]: float(r[1]) if r[1] is not None else None for r in cur.fetchall()}
    us_10 = curve.get("us_10y_yield"); us_2 = curve.get("us_2y_yield")
    jp_10 = curve.get("jp_10y_yield"); jp_s  = curve.get("jp_call_rate") or curve.get("jp_policy_rate")
    packet = {
        "as_of": _dt.date.today().isoformat(),
        "us_tiles": by_j.get("US", []),
        "jp_tiles": by_j.get("JP", []),
        "curve": {
            "us": {"2y": us_2, "10y": us_10, "2s10s_bp": ((us_10 - us_2) * 100) if (us_10 and us_2) else None},
            "jp": {"short": jp_s, "10y": jp_10, "spread_bp": ((jp_10 - jp_s) * 100) if (jp_10 and jp_s) else None},
        },
    }
    return packet


# ---------------------------------------------------------------------------
# Input hashing — skip when nothing changed
# ---------------------------------------------------------------------------

def _hash_payload(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _last_hash(scope: str, scope_key_like: str) -> str | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT input_hash FROM fact_macro_story
               WHERE scope=%s AND scope_key LIKE %s
               ORDER BY generated_at DESC LIMIT 1""",
            (scope, scope_key_like),
        )
        r = cur.fetchone()
        return r[0] if r else None


def _upsert_story(scope: str, scope_key: str, lang: str, text: str, model: str, input_hash: str, structured: dict | None = None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fact_macro_story (scope, scope_key, lang, generated_at, model, prompt_version, input_hash, text, structured_json)
            VALUES (%s, %s, %s, now(), %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (scope, scope_key, lang) DO UPDATE SET
                generated_at = now(),
                model = EXCLUDED.model,
                prompt_version = EXCLUDED.prompt_version,
                input_hash = EXCLUDED.input_hash,
                text = EXCLUDED.text,
                structured_json = EXCLUDED.structured_json
            """,
            (scope, scope_key, lang, model, PROMPT_VERSION, input_hash, text,
             json.dumps(structured, default=str, ensure_ascii=False) if structured else None),
        )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def generate_tile_captions(force: bool = False, per_juris: int = 8) -> int:
    if MACRO_BRIEF_DISABLED:
        logger.warning("MACRO_BRIEF_DISABLED — skipping tile-caption LLM call (no DB writes).")
        return 0
    prov = llm_providers.get(None)   # AI_ANALYST_LLM_PROVIDER, else DeepSeek
    api_key = resolve_env_key(prov.id)
    if not api_key:
        raise LLMError(f"No {prov.label} API key configured ({prov.env[0]})")
    tiles = _fetch_tiles_for_caption(limit_per_juris=per_juris)
    if not tiles:
        return 0
    digest = _hash_payload(tiles)
    if not force and _last_hash("tile", "tile:%") == digest:
        logger.info("tile captions: input unchanged, skipping LLM call")
        return 0

    user_prompt = json.dumps({"tiles": tiles}, ensure_ascii=False)
    payload = _chat_json_tolerant(
        api_key=api_key, provider=prov.id,
        system_prompt=MACRO_TILE_CAPTION_PROMPT, user_prompt=user_prompt,
        temperature=0.2, max_tokens=8000,
    )
    captions = payload.get("captions") or payload  # tolerate flat shape
    n = 0
    for slot, langs in captions.items():
        if not isinstance(langs, dict):
            continue
        for lang in ("en", "de"):
            txt = langs.get(lang)
            if not txt:
                continue
            _upsert_story("tile", f"tile:{slot}", lang, txt, llm_providers.chat_model(prov.id), digest)
            n += 1
    return n


def generate_macro_essay(session: str = "am", force: bool = False) -> int:
    if MACRO_BRIEF_DISABLED:
        logger.warning("MACRO_BRIEF_DISABLED — skipping essay LLM call (no DB writes).")
        return 0
    prov = llm_providers.get(None)   # AI_ANALYST_LLM_PROVIDER, else DeepSeek
    api_key = resolve_env_key(prov.id)
    if not api_key:
        raise LLMError(f"No {prov.label} API key configured ({prov.env[0]})")
    packet = _fetch_essay_packet()
    digest = _hash_payload(packet)
    today = _dt.date.today().isoformat()
    scope_key = f"essay:GLOBAL-{today}-{session}"
    if not force and _last_hash("essay", f"essay:GLOBAL-{today}-{session}") == digest:
        logger.info("essay: input unchanged, skipping LLM call")
        return 0

    payload = _chat_json_tolerant(
        api_key=api_key, provider=prov.id,
        system_prompt=MACRO_ESSAY_PROMPT,
        user_prompt=json.dumps(packet, ensure_ascii=False, default=str),
        temperature=0.4, max_tokens=2500,
    )
    n = 0
    for lang in ("en", "de"):
        text = payload.get(lang)
        if not text:
            continue
        _upsert_story("essay", scope_key, lang, text, llm_providers.chat_model(prov.id), digest, structured=packet)
        n += 1
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Generate bilingual macro stories")
    p.add_argument("--captions", action="store_true")
    p.add_argument("--essay", action="store_true")
    p.add_argument("--session", default="am", help="essay session tag (am/pm)")
    p.add_argument("--force", action="store_true", help="ignore input_hash cache")
    args = p.parse_args()
    if not args.captions and not args.essay:
        args.captions = args.essay = True

    out: dict[str, int] = {}
    try:
        if args.captions:
            out["captions_rows"] = generate_tile_captions(force=args.force)
        if args.essay:
            out["essay_rows"] = generate_macro_essay(session=args.session, force=args.force)
    except LLMError as e:
        print(f"macro_brief FAILED: {e}", file=sys.stderr)
        return 2
    print("macro_brief OK:", json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
