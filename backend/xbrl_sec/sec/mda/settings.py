from __future__ import annotations

import os
from pathlib import Path

from xbrl_sec.sec.settings import load_settings


_DEFAULT_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")


def forms_from_env() -> tuple[str, ...]:
    raw = os.environ.get("MZQA_MDA_FORMS")
    if not raw:
        return _DEFAULT_FORMS
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def lookback_years_from_env() -> int:
    try:
        return max(1, int(os.environ.get("MZQA_MDA_LOOKBACK_YEARS", "10")))
    except ValueError:
        return 10


def html_dir_from_env() -> Path:
    raw = os.environ.get("MZQA_MDA_XBRL_HTML_DIR")
    if raw:
        return Path(raw)
    return load_settings().market_data_root / "us_sec" / "xbrl_html"


def dirty_threshold(form_type: str) -> int:
    key = form_type.upper().replace("/", "A").replace("-", "")
    default = 500 if "10Q" in key else 2000
    try:
        return int(os.environ.get(f"MZQA_MDA_DIRTY_CHAR_THRESHOLD_FORM_{key}", str(default)))
    except ValueError:
        return default
