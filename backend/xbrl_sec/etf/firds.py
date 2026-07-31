"""ESMA FIRDS discovery, download, and ISO-20022 XML parsing (WA0006 §2.1, §4).

Discovery uses the public ESMA registers Solr file index; parsing is streaming
(ElementTree.iterparse) and namespace-agnostic (matches local element names) so
it tolerates auth.036.001.0x schema-version drift.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path

from xbrl_sec.sec.settings import load_settings
from .models import EtfRecord, ListingRecord
from .mics import resolve_venue

# Public ESMA registers Solr index of FIRDS files. Each doc carries
# file_name, file_type (FULINS/DLTINS), publication_date, download_link.
SOLR_URL = "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
_UA = "Mozilla/5.0 (MZQA ETF pipeline; +https://mzqa.example)"

# CFI category "C" = Collective Investment Vehicles. ESMA splits FULINS files by
# instrument letter; the C file is where ETFs (CIUs) live.
ETF_INSTRUMENT_LETTER = "C"


def _local(tag: str) -> str:
    """Strip the XML namespace, returning the local element name."""
    return tag.rsplit("}", 1)[-1]


def _http_json(url: str) -> dict:
    import json

    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_firds_files(
    file_type: str = "FULINS",
    instrument_letter: str | None = ETF_INSTRUMENT_LETTER,
    rows: int = 100,
) -> list[dict]:
    """Return FIRDS files of the given type, newest first.

    When instrument_letter is set, keep only files whose name contains
    `_<letter>_` (e.g. FULINS_20240608_C_1of2.zip).
    """
    params = {
        "q": "*:*",
        "fq": f"file_type:{file_type}",
        "wt": "json",
        "rows": str(rows),
        "sort": "publication_date desc",
    }
    url = f"{SOLR_URL}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    docs = data.get("response", {}).get("docs", [])
    if instrument_letter:
        token = f"_{instrument_letter.upper()}_"
        docs = [d for d in docs if token in (d.get("file_name") or "")]
    return docs


def _download_dir() -> Path:
    d = load_settings().project_root / ".cache" / "firds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_file(url: str, file_name: str | None = None) -> Path:
    """Stream a FIRDS ZIP to the local cache and return its path."""
    name = file_name or Path(urllib.parse.urlparse(url).path).name or "firds.zip"
    dest = _download_dir() / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)
    return dest


def _parse_refdata(elem: ET.Element) -> tuple[EtfRecord, str, str | None] | None:
    """Extract (EtfRecord, mic, trading_ccy) from one <RefData> element, or None."""
    gnl = None
    trading = None
    issr = None
    termntn = None
    for child in elem:
        name = _local(child.tag)
        if name == "FinInstrmGnlAttrbts":
            gnl = child
        elif name == "TradgVnRltdAttrbts":
            trading = child
        elif name == "Issr":
            issr = (child.text or "").strip() or None
        elif name == "TermntnDt":
            termntn = (child.text or "").strip() or None
    if gnl is None or trading is None:
        return None

    isin = full = short = cfi = ccy = None
    for g in gnl:
        n = _local(g.tag)
        if n == "Id":
            isin = (g.text or "").strip() or None
        elif n == "FullNm":
            full = (g.text or "").strip() or None
        elif n == "ShrtNm":
            short = (g.text or "").strip() or None
        elif n == "ClssfctnTp":
            cfi = (g.text or "").strip() or None
        elif n == "NtnlCcy":
            ccy = (g.text or "").strip() or None

    mic = None
    for tchild in trading:
        if _local(tchild.tag) == "Id":
            mic = (tchild.text or "").strip() or None
            break

    if not isin or not full or not mic:
        return None

    term_date: date | None = None
    if termntn:
        try:
            term_date = date.fromisoformat(termntn[:10])
        except ValueError:
            term_date = None

    rec = EtfRecord(
        isin=isin,
        full_name=full,
        short_name=short,
        issuer_lei=issr,
        fund_currency=ccy,
        cfi=cfi,
        termination_date=term_date,
    )
    return rec, mic, ccy


def parse_firds_zip(
    path: Path,
    limit: int | None = None,
    max_scan: int | None = None,
    etf_only: bool = True,
) -> tuple[list[EtfRecord], list[ListingRecord], int]:
    """Stream-parse a FIRDS ZIP, keeping ETFs admitted on DE/AT venues.

    A record is kept when its trading-venue segment MIC resolves to a DE/AT
    operating MIC (mics.resolve_venue) and, when `etf_only`, its CFI starts with
    "CE" (ISO 10962 Collective Investment Vehicle / Exchange-Traded Fund).

    Returns (unique EtfRecords, ListingRecords, instruments_scanned). `limit`
    caps unique ISINs kept; `max_scan` caps total records inspected (smoke runs).
    """
    etfs: dict[str, EtfRecord] = {}
    listings: dict[tuple[str, str], ListingRecord] = {}
    scanned = 0

    with zipfile.ZipFile(path) as zf:
        xml_members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        for member in xml_members:
            with zf.open(member) as fh:
                context = ET.iterparse(fh, events=("end",))
                for _event, elem in context:
                    if _local(elem.tag) != "RefData":
                        continue
                    scanned += 1
                    parsed = _parse_refdata(elem)
                    elem.clear()
                    if parsed is not None:
                        rec, seg_mic, ccy = parsed
                        is_etf = bool(rec.cfi) and rec.cfi[:2].upper() == "CE"
                        venue = resolve_venue(seg_mic)
                        if venue is not None and (is_etf or not etf_only):
                            op_mic, country = venue
                            etfs.setdefault(rec.isin, rec)
                            listings.setdefault(
                                (rec.isin, op_mic),
                                ListingRecord(rec.isin, op_mic, ccy, country),
                            )
                    if limit is not None and len(etfs) >= limit:
                        return list(etfs.values()), list(listings.values()), scanned
                    if max_scan is not None and scanned >= max_scan:
                        return list(etfs.values()), list(listings.values()), scanned

    return list(etfs.values()), list(listings.values()), scanned
