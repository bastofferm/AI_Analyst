"""Map ESMA FIRDS *segment* MICs to a DE/AT operating MIC + country.

FIRDS records the specific trading-venue segment MIC (e.g. FRAB, XETA, MUND,
XGAT, WBDM), not the operating MIC the spec's screener filters on (XETR, XFRA,
XWBO ...). German/Austrian venue segments share stable prefixes, so a small
prefix table resolves them without bundling the full ISO 10383 registry.
"""
from __future__ import annotations

# Order matters: longer / more specific prefixes first.
_PREFIX_RULES: tuple[tuple[str, str, str], ...] = (
    ("XETR", "XETR", "DE"), ("XETA", "XETR", "DE"), ("XETB", "XETR", "DE"),
    ("XETU", "XETR", "DE"), ("XETS", "XETR", "DE"), ("XET", "XETR", "DE"),   # Xetra
    ("XFRA", "XFRA", "DE"), ("FRA", "XFRA", "DE"),                            # Frankfurt
    ("XMUN", "XMUN", "DE"), ("MUN", "XMUN", "DE"),                            # Munich
    ("XGAT", "GETT", "DE"), ("GET", "GETT", "DE"),                            # gettex
    ("XSTU", "XSTU", "DE"), ("STU", "XSTU", "DE"), ("EUWX", "XSTU", "DE"),    # Stuttgart
    ("XDUS", "XDUS", "DE"), ("DUS", "XDUS", "DE"),                            # Düsseldorf
    ("XHAM", "XHAM", "DE"), ("HAM", "XHAM", "DE"),                            # Hamburg
    ("XHAN", "XHAN", "DE"), ("HAN", "XHAN", "DE"),                            # Hannover
    ("XBER", "XBER", "DE"), ("BER", "XBER", "DE"),                            # Berlin
    ("TGAT", "TGAT", "DE"), ("TGA", "TGAT", "DE"),                            # Tradegate
    ("XQTX", "XQTX", "DE"), ("QTX", "XQTX", "DE"),                            # Quotrix
    ("XWBO", "XWBO", "AT"), ("XVIE", "XWBO", "AT"), ("WB", "XWBO", "AT"),     # Wiener Börse
)


def resolve_venue(seg_mic: str | None) -> tuple[str, str] | None:
    """Return (operating_mic, country) for a DE/AT venue segment MIC, else None."""
    if not seg_mic:
        return None
    s = seg_mic.strip().upper()
    for prefix, op_mic, country in _PREFIX_RULES:
        if s.startswith(prefix):
            return op_mic, country
    return None
