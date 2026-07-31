from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: Path | str | None) -> str | None:
    if not path:
        return None
    target = Path(path)
    if not target.exists() or not target.is_file():
        return None
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extraction_quality(char_count: int, threshold: int) -> tuple[str, str | None]:
    if char_count <= 0:
        return "dirty", "empty_after_cleaning"
    if char_count < threshold:
        return "dirty", "below_char_threshold"
    return "clean", None
