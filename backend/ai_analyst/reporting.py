"""Two-page AI Analyst report generation."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from . import llm_runtime, services


# Outputs live inside the repo (MZQA-Equity-Terminal), not the sibling MZQA folder.
# <repo>/backend/ai_analyst/reporting.py -> parents[2] == repo root.
ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = ROOT / "output" / "ai_analyst"
PDFLATEX = Path(r"C:\Users\Bastian Offermann\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe")


def generate_report(
    ticker: str,
    *,
    api_key: str = "",
    base_url: str = llm_runtime.DEFAULT_BASE_URL,
    model: str = llm_runtime.DEFAULT_CHAT_MODEL,
) -> dict[str, Any]:
    ticker_clean = _safe_name(ticker.upper())
    out_dir = REPORT_ROOT / datetime.now().strftime("%Y%m%d") / ticker_clean
    out_dir.mkdir(parents=True, exist_ok=True)

    packet = services.report_data_packet(ticker_clean)
    data_path = out_dir / "data_packet.json"
    data_path.write_text(json.dumps(packet, indent=2, default=str), encoding="utf-8")

    narrative = _narrative_from_deepseek(packet, api_key, base_url, model) if api_key else _fallback_narrative(packet)
    tex = _render_tex(packet, narrative)
    tex_path = out_dir / "report.tex"
    tex_path.write_text(tex, encoding="utf-8")
    html_path = out_dir / "report.html"
    html_path.write_text(_render_html(packet, narrative), encoding="utf-8")

    pdf_path = out_dir / "report.pdf"
    pdf_ok = _compile_pdf(tex_path, out_dir)
    if not pdf_ok:
        pdf_ok = _write_simple_pdf(pdf_path, packet, narrative)
    return {
        "ticker": ticker_clean,
        "output_dir": str(out_dir),
        "data_packet": str(data_path),
        "tex": str(tex_path),
        "html": str(html_path),
        "pdf": str(pdf_path) if pdf_ok else None,
        "pdf_ok": pdf_ok,
        "message": "report generated" if pdf_ok else "report generated; PDF compile unavailable or failed",
    }


def _narrative_from_deepseek(packet: dict[str, Any], api_key: str, base_url: str, model: str) -> dict[str, str]:
    prompt = {
        "task": "Write compact analyst-style narrative only from this deterministic data packet. Do not invent numbers.",
        "sections": [
            "investment_summary",
            "financial_snapshot",
            "peer_positioning",
            "dcf_valuation",
            "factor_risk_profile",
            "caveats",
        ],
        "style": "plain institutional equity research, concise, maximum two page report when rendered",
        "data_packet": packet,
    }
    try:
        result = llm_runtime.chat_json(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt="You are a DeepSeek-only equity research writing assistant. Use only supplied data.",
            user_prompt=json.dumps(prompt, default=str)[:50000],
            temperature=0.15,
            max_tokens=1800,
        )
    except Exception as exc:
        fallback = _fallback_narrative(packet)
        fallback["caveats"] += f" DeepSeek narrative fallback used: {type(exc).__name__}."
        return fallback
    return {key: str(result.get(key) or "") for key in (
        "investment_summary",
        "financial_snapshot",
        "peer_positioning",
        "dcf_valuation",
        "factor_risk_profile",
        "caveats",
    )}


def _fallback_narrative(packet: dict[str, Any]) -> dict[str, str]:
    company = packet.get("company") or {}
    dcf = packet.get("dcf") or {}
    factor = (packet.get("factor_exposure") or {}).get("rows") or []
    factor_text = "No factor regression is available yet."
    if factor:
        row = factor[0]
        factor_text = (
            f"Latest {row.get('model')} window ends {row.get('window_end')}; "
            f"market beta is {_fmt(row.get('beta_mkt'))}, adjusted R2 is {_fmt(row.get('adj_r2'))}, "
            f"and quality score is {row.get('quality_score')}."
        )
    dcf_text = dcf.get("message") or (
        f"Corporate DCF baseline implies per-share value of {_fmt(dcf.get('per_share_value'))} "
        f"versus current price {_fmt(dcf.get('current_price'))}."
    )
    return {
        "investment_summary": f"{company.get('name') or packet.get('ticker')} is analysed using modeled statements, market metrics, peers, and factor regressions from xbrl_sec.",
        "financial_snapshot": "The financial snapshot uses standardized five-year line items and derived metrics, not raw XBRL facts directly.",
        "peer_positioning": "Peers are selected deterministically from the same sector or mapping sector, same jurisdiction first, and ranked by latest market capitalization.",
        "dcf_valuation": dcf_text,
        "factor_risk_profile": factor_text,
        "caveats": "This is a compact research note; raw XBRL paths are supporting evidence and sector-specific valuation models for banks, insurers, REITs, and asset managers are gated.",
    }


def _render_tex(packet: dict[str, Any], narrative: dict[str, str]) -> str:
    company = packet.get("company") or {}
    peers = (packet.get("peer_group") or {}).get("peers") or []
    dcf = packet.get("dcf") or {}
    factor_rows = (packet.get("factor_exposure") or {}).get("rows") or []
    peer_lines = "\n".join(
        f"{_tex(p.get('ticker'))} & {_tex(p.get('name'))} & {_tex(_money(p.get('market_cap')))} & {_tex(_pct(p.get('revenue_growth')))} & {_tex(_x(p.get('ev_ebitda')))} \\\\"
        for p in peers[:5]
    ) or r"\multicolumn{5}{l}{No peer data available.}\\"
    factor_line = "No factor data available."
    if factor_rows:
        f = factor_rows[0]
        factor_line = (
            f"{f.get('model')} beta MKT {_fmt(f.get('beta_mkt'))}, SMB {_fmt(f.get('beta_smb'))}, "
            f"HML {_fmt(f.get('beta_hml'))}, MOM {_fmt(f.get('beta_mom'))}, "
            f"adj. R2 {_fmt(f.get('adj_r2'))}, residual vol {_pct(f.get('residual_vol'))}, "
            f"quality {f.get('quality_score')}/5."
        )
    return rf"""\documentclass[9pt]{{article}}
\usepackage[margin=0.45in]{{geometry}}
\usepackage{{booktabs,tabularx,xcolor}}
\usepackage{{helvet}}
\renewcommand{{\familydefault}}{{\sfdefault}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{3pt}}
\begin{{document}}
\pagestyle{{empty}}
{{\Large \textbf{{{_tex(company.get('name') or packet.get('ticker'))}}}}} \hfill {_tex(datetime.now().strftime('%Y-%m-%d'))}\\
{{\small {_tex(company.get('ticker'))} | {_tex(company.get('jurisdiction'))} | {_tex(company.get('gics_sector_name') or company.get('mapping_sector'))}}}

\section*{{Investment Summary}}
{_tex(narrative.get('investment_summary'))}

\section*{{Financial Snapshot}}
{_tex(narrative.get('financial_snapshot'))}

\section*{{Peer Group}}
{_tex(narrative.get('peer_positioning'))}
\begin{{tabularx}}{{\linewidth}}{{llrrr}}
\toprule
Ticker & Name & Market Cap & Rev. Growth & EV/EBITDA\\
\midrule
{peer_lines}
\bottomrule
\end{{tabularx}}

\section*{{DCF Valuation}}
{_tex(narrative.get('dcf_valuation'))}\\
\textbf{{Per-share value:}} {_tex(_money(dcf.get('per_share_value')))} \quad
\textbf{{Current price:}} {_tex(_money(dcf.get('current_price')))} \quad
\textbf{{Upside:}} {_tex(_pct(dcf.get('upside_pct'), already_pct=True))}

\section*{{Factor/Risk Profile}}
{_tex(narrative.get('factor_risk_profile'))}\\
{{\small {_tex(factor_line)}}}

\section*{{Caveats}}
{{\small {_tex(narrative.get('caveats'))}}}
\end{{document}}
"""


def _render_html(packet: dict[str, Any], narrative: dict[str, str]) -> str:
    company = packet.get("company") or {}
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{company.get('ticker')} AI Analyst Report</title>
<style>body{{font-family:Arial,sans-serif;max-width:820px;margin:28px auto;color:#111827}}h1{{margin-bottom:0}}h2{{font-size:15px;margin-top:18px}}p{{font-size:12px;line-height:1.45}}</style></head><body>
<h1>{_html(company.get('name') or packet.get('ticker'))}</h1>
<p>{_html(company.get('ticker'))} | {_html(company.get('jurisdiction'))} | {_html(company.get('gics_sector_name') or company.get('mapping_sector'))}</p>
{''.join(f'<h2>{_html(k.replace("_"," ").title())}</h2><p>{_html(v)}</p>' for k, v in narrative.items())}
</body></html>"""


def _compile_pdf(tex_path: Path, out_dir: Path) -> bool:
    try:
        if not PDFLATEX.exists():
            return False
    except OSError:
        return False
    try:
        subprocess.run(
            [str(PDFLATEX), "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=str(out_dir),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        return False
    return (out_dir / "report.pdf").exists()


def _write_simple_pdf(path: Path, packet: dict[str, Any], narrative: dict[str, str]) -> bool:
    """Tiny fallback PDF writer using built-in Helvetica, capped at two pages."""
    company = packet.get("company") or {}
    title = company.get("name") or packet.get("ticker") or "AI Analyst Report"
    lines = [
        str(title),
        f"{company.get('ticker', '')} | {company.get('jurisdiction', '')} | {company.get('gics_sector_name') or company.get('mapping_sector') or ''}",
        "",
    ]
    for key, text in narrative.items():
        lines.append(key.replace("_", " ").title())
        lines.extend(_wrap(str(text or ""), 92))
        lines.append("")
    peers = ((packet.get("peer_group") or {}).get("peers") or [])[:5]
    if peers:
        lines.append("Peer Group")
        for peer in peers:
            lines.append(f"{peer.get('ticker')}  {peer.get('name')}  Market cap {_money(peer.get('market_cap'))}  EV/EBITDA {_x(peer.get('ev_ebitda'))}")
    max_lines_per_page = 48
    pages = [lines[:max_lines_per_page], lines[max_lines_per_page:max_lines_per_page * 2]]
    pages = [p for p in pages if p]
    objects: list[bytes] = []
    page_ids: list[int] = []
    font_id = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page in pages:
        content_id = len(objects) + 2
        page_id = len(objects) + 1
        page_ids.append(page_id)
        stream = _pdf_stream(page)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode("latin-1"))
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{idx} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    try:
        path.write_bytes(bytes(data))
    except OSError:
        return False
    return True


def _pdf_stream(lines: list[str]) -> bytes:
    commands = ["BT", "/F1 9 Tf", "50 750 Td", "12 TL"]
    for idx, line in enumerate(lines):
        if idx == 0:
            commands.append("/F1 15 Tf")
        elif idx == 1:
            commands.append("/F1 9 Tf")
        commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1", errors="replace")


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        if len(trial) > width and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "UNKNOWN")


def _tex(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def _html(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "n/a"


def _money(value: Any) -> str:
    try:
        v = float(value)
    except Exception:
        return "n/a"
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    return f"{v:.2f}"


def _pct(value: Any, *, already_pct: bool = False) -> str:
    try:
        v = float(value)
    except Exception:
        return "n/a"
    if not already_pct:
        v *= 100.0
    return f"{v:.2f}%"


def _x(value: Any) -> str:
    try:
        return f"{float(value):.2f}x"
    except Exception:
        return "n/a"
