"""Qwen reasoning through a local Ollama server."""
from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from xbrl_sec.sec.news.sentiment.prompts import SYSTEM_PROMPT, user_prompt
from xbrl_sec.sec.settings import load_settings


class QwenOllamaBackend:
    model_key = "qwen"

    def reason(self, *, ticker: str, title: str, text: str) -> dict[str, object]:
        settings = load_settings()
        payload = {
            "model": settings.news_qwen_model,
            "stream": False,
            "format": "json",
            "prompt": f"{SYSTEM_PROMPT}\n\n{user_prompt(ticker=ticker, title=title, text=text)}",
            "options": {"temperature": 0.1},
        }
        request = Request(
            f"{settings.news_ollama_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=120) as response:
            outer = json.loads(response.read().decode("utf-8"))
        return _parse_result(str(outer.get("response") or "{}"))


def _parse_result(value: str) -> dict[str, object]:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.S)
        if not match:
            raise RuntimeError("Ollama did not return a JSON object")
        return json.loads(match.group(0))
