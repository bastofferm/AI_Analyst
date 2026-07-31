"""On-disk cache for non-API macro downloads.

Everything that arrives as an Excel / CSV / HTML scrape from a central-bank
or statistical-office website lands under ``D:/macroData/`` instead of in
the repo. Pure JSON/SDMX APIs (FRED, ECB) do NOT use this cache — those
just hit their endpoints directly and stream straight into postgres.

Layout (see D:/macroData/README.md for the canonical reference):

    D:/macroData/
    ├── raw/{source}/{slug(native_id)}/YYYY-MM-DD_HHMMSS.{ext}
    ├── latest/{source}/{slug(native_id)}.{ext}
    ├── drops/{source}/{native_id}.csv
    └── manifest/{source}/{slug(native_id)}.json

Public API:
    fetch(source, native_id, url, ext, attempts=3, ua=None)
        → bytes | None  — downloads, persists raw + latest + manifest.
    read_drop(source, native_id) → bytes | None
        — operator-maintained fallback.
    cache_root() / drops_dir() / latest_path() — for callers that need
        the on-disk locations.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("mzqa.macro_cache")

# ---------------------------------------------------------------------------
# Cache root resolution
# ---------------------------------------------------------------------------

DEFAULT_CACHE_ROOT = Path(r"D:\macroData")


def cache_root() -> Path:
    """Resolve the cache root from $MZQA_MACRO_CACHE or the default D:/macroData."""
    return Path(os.environ.get("MZQA_MACRO_CACHE", str(DEFAULT_CACHE_ROOT)))


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def raw_dir(source: str, native_id: str) -> Path:
    return _ensure_dir(cache_root() / "raw" / source / _slug(native_id))


def latest_dir(source: str) -> Path:
    return _ensure_dir(cache_root() / "latest" / source)


def drops_dir(source: str | None = None) -> Path:
    base = cache_root() / "drops"
    if source:
        return _ensure_dir(base / source)
    return _ensure_dir(base)


def manifest_dir(source: str) -> Path:
    return _ensure_dir(cache_root() / "manifest" / source)


def latest_path(source: str, native_id: str, ext: str) -> Path:
    return latest_dir(source) / f"{_slug(native_id)}.{ext.lstrip('.')}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_\-]+", "_", s.lower()).strip("_") or "unnamed"


# ---------------------------------------------------------------------------
# HTTP fetching
# ---------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# HTTP statuses worth retrying (origin-down + transient 5xx).
_RETRY_STATUSES = {500, 502, 503, 504, 521, 522, 523, 524}


def _http_get_with_retry(url: str, attempts: int, ua: str | None) -> tuple[bytes | None, int | None]:
    """Fetch *url* with exponential backoff. Returns (bytes_or_None, http_status_or_None)."""
    delay = 1.0
    last_status: int | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": ua or _BROWSER_UA,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            if exc.code not in _RETRY_STATUSES or i == attempts - 1:
                logger.warning("macro_cache: HTTP %s on %s (no further retry)", exc.code, url)
                return None, exc.code
            logger.info("macro_cache: HTTP %s on %s, retrying in %.1fs", exc.code, url, delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            if i == attempts - 1:
                logger.warning("macro_cache: network error on %s: %s (no further retry)", url, exc)
                return None, None
            logger.info("macro_cache: network error on %s: %s, retrying in %.1fs", url, exc, delay)
        time.sleep(delay)
        delay *= 4
    return None, last_status


def fetch(
    source: str,
    native_id: str,
    url: str,
    ext: str,
    attempts: int = 3,
    ua: str | None = None,
) -> bytes | None:
    """Download *url*, persist to ``D:/macroData/{raw,latest,manifest}``, return bytes.

    On HTTP / network failure: returns ``None`` and does NOT touch the cache —
    the previous latest/raw remain intact, so callers can fall through to
    ``read_drop()`` or to the prior cached copy if they want.
    """
    raw, status = _http_get_with_retry(url, attempts=attempts, ua=ua)
    if not raw:
        _write_manifest(source, native_id, url, ext, status=status, bytes_=0, sha=None, error=True)
        return None

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    raw_path = raw_dir(source, native_id) / f"{ts}.{ext.lstrip('.')}"
    raw_path.write_bytes(raw)

    latest_p = latest_path(source, native_id, ext)
    shutil.copyfile(raw_path, latest_p)

    sha = hashlib.sha256(raw).hexdigest()
    _write_manifest(source, native_id, url, ext, status=status, bytes_=len(raw), sha=sha, error=False)
    logger.info("macro_cache: cached %s/%s (%d bytes) → %s", source, native_id, len(raw), latest_p)
    return raw


def _write_manifest(
    source: str,
    native_id: str,
    url: str,
    ext: str,
    status: int | None,
    bytes_: int,
    sha: str | None,
    error: bool,
) -> None:
    p = manifest_dir(source) / f"{_slug(native_id)}.json"
    payload = {
        "source": source,
        "native_id": native_id,
        "url": url,
        "ext": ext.lstrip("."),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "http_status": status,
        "bytes": bytes_,
        "sha256": sha,
        "ok": not error,
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Operator drop reads
# ---------------------------------------------------------------------------


def read_drop(source: str, native_id: str) -> bytes | None:
    """Return the contents of ``drops/{source}/{native_id}.csv`` if it exists."""
    p = drops_dir(source) / f"{native_id}.csv"
    if not p.exists():
        # Tolerate a legacy un-namespaced drop file.
        legacy = cache_root() / "drops" / f"{native_id}.csv"
        if legacy.exists():
            return legacy.read_bytes()
        return None
    return p.read_bytes()


def read_latest(source: str, native_id: str, ext: str) -> bytes | None:
    """Read the most-recent successfully-cached copy (no network)."""
    p = latest_path(source, native_id, ext)
    if not p.exists():
        return None
    return p.read_bytes()
