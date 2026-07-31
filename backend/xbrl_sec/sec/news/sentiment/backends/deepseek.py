"""DeepSeek cloud reasoning backend."""
from __future__ import annotations

import json
import os
import re
from urllib.request import Request, urlopen

from ai_analyst.llm_runtime import resolve_env_key
from xbrl_sec.sec.news.sentiment.prompts import SYSTEM_PROMPT, user_prompt
from xbrl_sec.sec.settings import load_settings


class DeepSeekBackend:
    model_key = "deepseek"

    def reason(self, *, ticker: str, title: str, text: str) -> dict[str, object]:
        api_key = resolve_env_key()
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        payload = {
            "model": load_settings().news_deepseek_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(ticker=ticker, title=title, text=text)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        request = Request(
            os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = str(result["choices"][0]["message"]["content"])
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                raise RuntimeError("DeepSeek did not return a JSON object")
            return json.loads(match.group(0))
