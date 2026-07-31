from __future__ import annotations

from pathlib import Path


def read_html(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "windows-1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
