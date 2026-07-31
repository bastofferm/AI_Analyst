from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

from xbrl_sec.sec.settings import load_settings


SEC_USER_AGENT = os.environ.get(
    "MZQA_SEC_USER_AGENT",
    "MZQA XBRL pipeline contact=bastian.offermann@gmail.com",
)


def sec_request(url: str) -> Request:
    return Request(url, headers={"User-Agent": SEC_USER_AGENT})


def sec_sleep() -> None:
    time.sleep(float(os.environ.get("MZQA_SEC_DELAY_SECONDS", "0.13")))


def download_url(url: str, dest: Path, force: bool = False) -> tuple[bool, str | None]:
    if dest.exists() and not force:
        return False, None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(sec_request(url), timeout=90) as response:
            dest.write_bytes(response.read())
        sec_sleep()
        return True, None
    except Exception as exc:
        return False, str(exc)[:1000]


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_cik(value: str | int | None) -> str:
    if value is None:
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits.zfill(10) if digits else ""


def clean_accession(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def dashed_accession(value: str | None) -> str:
    clean = clean_accession(value)
    if len(clean) == 18:
        return f"{clean[:10]}-{clean[10:12]}-{clean[12:]}"
    return value or ""


def us_sec_root() -> Path:
    return load_settings().market_data_root / "us_sec"


def parse_date(value: str | None):
    from datetime import date, datetime

    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            if fmt == "%Y-%m-%d":
                return date.fromisoformat(text[:10])
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except Exception:
            pass
    for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(text.upper(), fmt).date()
        except Exception:
            pass
    return None
