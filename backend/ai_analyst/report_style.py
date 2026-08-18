"""Shared MZQA report styling and the HTML-to-PDF renderer.

Extracted from ``ai_analyst.committee.report_pdf`` so the committee memo and the quant
research dossier render in one house style and share one Chrome-discovery path, rather than
each carrying its own copy of the palette and the subprocess invocation.

The design tokens mirror ``frontend/src/app/globals.css``, so a printed report and the web
terminal are visibly the same product. PDF generation goes through headless Chrome/Edge
``--print-to-pdf``, which means CSS fidelity is exactly the browser's — no separate print
engine to keep in sync.
"""
from __future__ import annotations

import html as _html
import shutil
import subprocess
from pathlib import Path

# --- MZQA design tokens (from frontend/src/app/globals.css) ---
BG = "#F5F4F0"
PANEL = "#FBFAF7"
NAVY = "#2F4D73"
NAVY2 = "#476D99"
NAVY3 = "#6B86A8"
MUTED = "#6F7890"
BORDER = "#DDD8CD"
BORDER_SOFT = "#EEECE5"
GREEN = "#1F7A52"
RED = "#8C2F39"
AMBER = "#B7791F"

_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str | None:
    """A headless-capable Chromium binary, or None if the box has neither Chrome nor Edge."""
    for cand in _CHROME_CANDIDATES:
        if Path(cand).exists():
            return cand
    return shutil.which("chrome") or shutil.which("msedge")


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render ``html_path`` to ``pdf_path``. Returns False when no browser is available.

    Never raises: a missing browser degrades the caller to serving the HTML, which is
    readable in its own right, rather than failing the request.
    """
    browser = find_browser()
    if not browser:
        return False
    url = "file:///" + str(Path(html_path).resolve()).replace("\\", "/")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-pdf-header-footer", "--virtual-time-budget=4000",
        f"--print-to-pdf={pdf_path}", url,
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=90)
    except Exception:  # noqa: BLE001 - PDF rendering is best-effort by design
        return False
    return Path(pdf_path).exists()


def esc(value: object) -> str:
    """HTML-escape any value for interpolation into a template."""
    return _html.escape("" if value is None else str(value))
