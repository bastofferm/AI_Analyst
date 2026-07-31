"""Dash callbacks for the AI Analyst panel.

All callbacks are registered via :func:`register_ai_callbacks` so this module can be
imported lazily from ops_dashboard.py without side-effects at import time.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import dash
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update
from flask import abort, send_file

from . import _theme as T
from . import llm_runtime, render_spec, tools as ai_tools, reporting
from . import dcf_engine
from .prompts import CHAT_SYSTEM_PROMPT, DCF_SYSTEM_PROMPT, DCF_NARRATIVE_PROMPT


def register_ai_callbacks(app: dash.Dash) -> None:
    if "ai_analyst_report" not in app.server.view_functions:
        @app.server.route("/ai-analyst-report/<path:relpath>", endpoint="ai_analyst_report")
        def _serve_ai_report(relpath: str):
            root = reporting.REPORT_ROOT.resolve()
            target = (root / relpath).resolve()
            if root not in target.parents and target != root:
                abort(404)
            if not target.exists() or not target.is_file():
                abort(404)
            return send_file(str(target))

    # ── Panel open/close + tab switching ─────────────────────────────────────
    @app.callback(
        Output("ai-panel-card", "style"),
        Output("ai-panel-pill", "style"),
        Output("ai-ui-state", "data"),
        Output("ai-view-chat", "style"),
        Output("ai-view-dcf", "style"),
        Output("ai-view-report", "style"),
        Output("ai-view-settings", "style"),
        Input("ai-panel-pill", "n_clicks"),
        Input("ai-min-btn", "n_clicks"),
        Input("ai-close-btn", "n_clicks"),
        Input("ai-tab-chat", "n_clicks"),
        Input("ai-tab-dcf", "n_clicks"),
        Input("ai-tab-report", "n_clicks"),
        Input("ai-tab-settings", "n_clicks"),
        State("ai-ui-state", "data"),
        prevent_initial_call=False,
    )
    def _toggle_panel(_p, _m, _c, _t1, _t2, _t3, _t4, ui_state):
        ui_state = ui_state or {"open": False, "tab": "chat"}
        trigger = ctx.triggered_id
        if trigger == "ai-panel-pill":
            ui_state["open"] = True
        elif trigger in ("ai-min-btn", "ai-close-btn"):
            ui_state["open"] = False
        elif trigger == "ai-tab-chat":
            ui_state["tab"] = "chat"
            ui_state["open"] = True
        elif trigger == "ai-tab-dcf":
            ui_state["tab"] = "dcf"
            ui_state["open"] = True
        elif trigger == "ai-tab-report":
            ui_state["tab"] = "report"
            ui_state["open"] = True
        elif trigger == "ai-tab-settings":
            ui_state["tab"] = "settings"
            ui_state["open"] = True

        is_open = bool(ui_state.get("open"))
        tab     = ui_state.get("tab") or "chat"
        card_style = {"display": "flex"} if is_open else {"display": "none"}
        pill_style = {"display": "none"} if is_open else {}
        chat_style = ({"display": "flex", "flexDirection": "column", "flex": "1 1 auto", "minHeight": "0"}
                       if tab == "chat" else {"display": "none"})
        dcf_style  = ({"display": "flex", "flexDirection": "column", "flex": "1 1 auto",
                        "minHeight": "0"} if tab == "dcf" else {"display": "none"})
        report_style  = ({"display": "flex", "flexDirection": "column", "flex": "1 1 auto",
                           "minHeight": "0"} if tab == "report" else {"display": "none"})
        set_style  = ({"display": "block", "flex": "1 1 auto", "overflowY": "auto"}
                       if tab == "settings" else {"display": "none"})
        return card_style, pill_style, ui_state, chat_style, dcf_style, report_style, set_style

    # ── Tab button "active" class ────────────────────────────────────────────
    @app.callback(
        Output("ai-tab-chat", "className"),
        Output("ai-tab-dcf", "className"),
        Output("ai-tab-report", "className"),
        Output("ai-tab-settings", "className"),
        Input("ai-ui-state", "data"),
    )
    def _tab_classes(ui_state):
        tab = (ui_state or {}).get("tab", "chat")
        def c(name): return "ai-tab-btn active" if tab == name else "ai-tab-btn"
        return c("chat"), c("dcf"), c("report"), c("settings")

    # ── Context bridge: dashboard ticker → AI context store ──────────────────
    @app.callback(
        Output("ai-context-store", "data"),
        Output("ai-context-chip", "children"),
        Input("stock-ticker", "value"),
        Input("jp-ticker", "value"),
        Input("dd-jurisdiction-tabs", "active_tab"),
    )
    def _bind_context(us_ticker, jp_ticker, juris_tab):
        is_jp = (juris_tab == "dd-jp")
        ticker = jp_ticker if is_jp else us_ticker
        juris  = "JP" if is_jp else "US"
        chip = f"Context: {ticker or '—'} · {juris}"
        return {"ticker": ticker, "jurisdiction": juris}, chip

    # ── Settings store + key status ──────────────────────────────────────────
    @app.callback(
        Output("ai-settings", "data"),
        Output("ai-api-key", "data"),
        Output("ai-settings-status", "children"),
        Input("ai-settings-key", "value"),
        Input("ai-settings-chat-model", "value"),
        Input("ai-settings-reasoner-model", "value"),
        Input("ai-settings-temperature", "value"),
        Input("ai-settings-max-tokens", "value"),
        Input("ai-settings-base-url", "value"),
    )
    def _persist_settings(key, chat_model, reasoner_model, temp, max_tok, base_url):
        settings = {
            "chat_model":     (chat_model or llm_runtime.DEFAULT_CHAT_MODEL).strip(),
            "reasoner_model": (reasoner_model or llm_runtime.DEFAULT_REASONER).strip(),
            "temperature":    float(temp) if temp is not None else 0.2,
            "max_tokens":     int(max_tok) if max_tok else 2000,
            "base_url":       (base_url or llm_runtime.DEFAULT_BASE_URL).strip(),
        }
        env_key = llm_runtime.resolve_env_key()
        if key and key.strip():
            status = "Key set · source: session"
        elif env_key:
            status = "Key set · source: environment"
        else:
            status = "No key configured"
        return settings, (key.strip() if key else None), status

    # ── Quick prompt buttons → input text ────────────────────────────────────
    @app.callback(
        Output("ai-chat-input", "value", allow_duplicate=True),
        Input({"type": "ai-quick-prompt", "index": ALL}, "n_clicks"),
        State({"type": "ai-quick-prompt", "index": ALL}, "children"),
        prevent_initial_call=True,
    )
    def _quick_prompt(clicks, labels):
        if not any(clicks or []):
            raise dash.exceptions.PreventUpdate
        trig = ctx.triggered_id
        if not isinstance(trig, dict):
            raise dash.exceptions.PreventUpdate
        idx = trig.get("index")
        try:
            return labels[idx]
        except (IndexError, TypeError):
            raise dash.exceptions.PreventUpdate

    # ── Reset conversation ───────────────────────────────────────────────────
    @app.callback(
        Output("ai-chat-history", "data", allow_duplicate=True),
        Output("ai-chat-bubbles", "data", allow_duplicate=True),
        Output("ai-settings-reset-flash", "children"),
        Input("ai-settings-reset", "n_clicks"),
        Input("ai-chat-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def _reset_chat(_a, _b):
        return [], [], "Conversation cleared."

    # ── Send: push user bubble + arm pending-msg ─────────────────────────────
    @app.callback(
        Output("ai-chat-bubbles", "data", allow_duplicate=True),
        Output("ai-pending-msg", "data"),
        Output("ai-chat-input", "value"),
        Output("ai-chat-thinking", "children", allow_duplicate=True),
        Input("ai-chat-send", "n_clicks"),
        State("ai-chat-input", "value"),
        State("ai-chat-bubbles", "data"),
        State("ai-context-store", "data"),
        prevent_initial_call=True,
    )
    def _on_send(_n, text, bubbles, ctx_data):
        text = (text or "").strip()
        if not text:
            raise dash.exceptions.PreventUpdate
        bubbles = list(bubbles or [])
        bubbles.append({"role": "user", "text": text})
        thinking = html.Span([
            html.Span(className="ai-thinking-dot"),
            html.Span(className="ai-thinking-dot"),
            html.Span(className="ai-thinking-dot"),
            html.Span("  thinking…", style={"marginLeft": "4px"}),
        ])
        return bubbles, {"text": text, "context": ctx_data or {}, "t": time.time()}, "", thinking

    # ── Run LLM: triggered by pending-msg ────────────────────────────────────
    @app.callback(
        Output("ai-chat-bubbles", "data", allow_duplicate=True),
        Output("ai-chat-history", "data", allow_duplicate=True),
        Output("ai-chat-thinking", "children", allow_duplicate=True),
        Input("ai-pending-msg", "data"),
        State("ai-chat-bubbles", "data"),
        State("ai-chat-history", "data"),
        State("ai-api-key", "data"),
        State("ai-settings", "data"),
        prevent_initial_call=True,
    )
    def _run_llm(pending, bubbles, history, ui_key, settings):
        if not pending or not pending.get("text"):
            raise dash.exceptions.PreventUpdate
        text = pending["text"]
        ctx_data = pending.get("context") or {}
        bubbles = list(bubbles or [])
        history = list(history or [])

        api_key = (ui_key or "").strip() or llm_runtime.resolve_env_key()
        if not api_key:
            bubbles.append({"role": "error",
                             "text": "No DeepSeek API key configured. Open Settings and paste one, "
                                     "or set DEEPSEEK_API_KEY in your environment."})
            return bubbles, history, ""

        s = settings or {}
        base_url = s.get("base_url") or llm_runtime.DEFAULT_BASE_URL
        model    = s.get("chat_model") or llm_runtime.DEFAULT_CHAT_MODEL
        temp     = float(s.get("temperature", 0.2))
        max_tok  = int(s.get("max_tokens", 2000))

        ticker = ctx_data.get("ticker")
        juris  = ctx_data.get("jurisdiction") or "US"
        context_hint = (f"default_ticker={ticker} jurisdiction={juris}"
                        if ticker else f"default_ticker=<none> jurisdiction={juris}")
        sys_prompt = CHAT_SYSTEM_PROMPT + f"\n\nDASHBOARD CONTEXT: {context_hint}"

        try:
            content, trace = llm_runtime.chat_with_tools(
                api_key=api_key, base_url=base_url, model=model,
                system_prompt=sys_prompt,
                user_prompt=text,
                tools=ai_tools.TOOLS,
                tool_executor=ai_tools.execute,
                history=history,
                temperature=temp,
                max_tokens=max_tok,
            )
        except llm_runtime.LLMError as e:
            bubbles.append({"role": "error", "text": str(e)})
            return bubbles, history, ""
        except Exception as e:
            bubbles.append({"role": "error", "text": f"{type(e).__name__}: {e}"})
            return bubbles, history, ""

        narrative, _visuals = llm_runtime.parse_assistant_payload(content)
        bubbles.append({
            "role":     "assistant",
            "text":     narrative or content,
            "visuals":  [],
            "tools":    [],
        })
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": content})
        history = history[-20:]
        return bubbles, history, ""

    # ── Render chat bubbles from store ───────────────────────────────────────
    @app.callback(
        Output("ai-chat-messages", "children"),
        Input("ai-chat-bubbles", "data"),
    )
    def _render_bubbles(bubbles):
        bubbles = bubbles or []
        if not bubbles:
            return [html.Div(
                "Ask anything about the fundamentals of any US or Japanese company in the warehouse. "
                "Examples: 'Compare AAPL and 6758 on EBITDA margin', "
                "'Top 10 US firms by ROIC in FY2024'.",
                style={"fontSize": "11px", "color": T.MUTED, "fontStyle": "italic",
                       "padding": "12px", "lineHeight": "1.5"})]
        out = []
        for b in bubbles:
            role = b.get("role", "assistant")
            text = b.get("text") or ""
            cls = f"ai-bubble {role}"
            children: list[Any] = []
            if role == "assistant":
                children.append(dcc.Markdown(text, link_target="_blank",
                                              style={"margin": 0}))
            else:
                children.append(html.Span(text))
            out.append(html.Div(children, className=cls))
        return out

    # ── DCF: ask LLM for assumptions ─────────────────────────────────────────
    @app.callback(
        Output("ai-dcf-g1", "value"),
        Output("ai-dcf-g2", "value"),
        Output("ai-dcf-g3", "value"),
        Output("ai-dcf-g4", "value"),
        Output("ai-dcf-g5", "value"),
        Output("ai-dcf-term-g", "value"),
        Output("ai-dcf-ebit-margin", "value"),
        Output("ai-dcf-tax-rate", "value"),
        Output("ai-dcf-capex", "value"),
        Output("ai-dcf-nwc", "value"),
        Output("ai-dcf-wacc", "value"),
        Output("ai-dcf-shares", "value"),
        Output("ai-dcf-output", "children", allow_duplicate=True),
        Input("ai-dcf-ask-btn", "n_clicks"),
        State("ai-context-store", "data"),
        State("ai-api-key", "data"),
        State("ai-settings", "data"),
        prevent_initial_call=True,
    )
    def _dcf_ask(_n, ctx_data, ui_key, settings):
        ctx_data = ctx_data or {}
        ticker = ctx_data.get("ticker")
        if not ticker:
            return (no_update,)*12 + (_error_box("Select a ticker in the Deep Dive tab first."),)
        api_key = (ui_key or "").strip() or llm_runtime.resolve_env_key()
        if not api_key:
            return (no_update,)*12 + (_error_box("Add a DeepSeek API key in Settings."),)
        s = settings or {}
        base_url = s.get("base_url") or llm_runtime.DEFAULT_BASE_URL
        model    = s.get("reasoner_model") or llm_runtime.DEFAULT_REASONER

        overview = ai_tools.get_company_overview(ticker)
        fund     = ai_tools.get_fundamentals(ticker)
        metrics  = ai_tools.get_metrics(ticker)
        user_prompt = (
            f"Company: {ticker} ({overview.get('name','?')}) — sector: "
            f"{overview.get('gics_sector_name','?')}.\n\n"
            f"Historical fundamentals (most recent fiscal years):\n"
            f"{json.dumps(fund, default=str)[:6000]}\n\n"
            f"Derived metrics:\n{json.dumps(metrics, default=str)[:4000]}\n\n"
            f"Propose DCF assumptions per the schema. Return JSON only."
        )
        try:
            payload = llm_runtime.chat_json(
                api_key=api_key, base_url=base_url, model=model,
                system_prompt=DCF_SYSTEM_PROMPT, user_prompt=user_prompt,
                temperature=0.1, max_tokens=1200,
            )
        except llm_runtime.LLMError as e:
            return (no_update,)*12 + (_error_box(str(e)),)

        a = dcf_engine.normalise_assumptions(payload)
        g = a["rev_growth_pct"]
        flash = html.Div([
            html.Div("Assumptions filled — review and click Run DCF.",
                      style={"color": T.GREEN, "fontSize": "11px", "fontWeight": "600"}),
            html.Div(a.get("rationale") or "", style={"fontSize": "11px", "color": T.MUTED,
                                                       "marginTop": "4px", "lineHeight": "1.5"}),
        ], style={"padding": "8px 0"})
        shares = a["share_count_mm"] or no_update
        return (g[0], g[1], g[2], g[3], g[4],
                a["terminal_growth_pct"], a["ebit_margin_pct"], a["tax_rate_pct"],
                a["capex_pct_of_rev"], a["nwc_pct_of_rev"], a["wacc_pct"],
                shares, flash)

    # ── DCF: run computation ─────────────────────────────────────────────────
    @app.callback(
        Output("ai-dcf-output", "children"),
        Input("ai-dcf-run-btn", "n_clicks"),
        State("ai-dcf-g1", "value"), State("ai-dcf-g2", "value"),
        State("ai-dcf-g3", "value"), State("ai-dcf-g4", "value"),
        State("ai-dcf-g5", "value"),
        State("ai-dcf-term-g", "value"),
        State("ai-dcf-ebit-margin", "value"),
        State("ai-dcf-tax-rate", "value"),
        State("ai-dcf-capex", "value"),
        State("ai-dcf-nwc", "value"),
        State("ai-dcf-wacc", "value"),
        State("ai-dcf-shares", "value"),
        State("ai-dcf-px", "value"),
        State("ai-context-store", "data"),
        State("ai-api-key", "data"),
        State("ai-settings", "data"),
        prevent_initial_call=True,
    )
    def _dcf_run(_n, g1, g2, g3, g4, g5, term_g, ebit_m, tax_r, capex, nwc, wacc, shares, px,
                  ctx_data, ui_key, settings):
        ctx_data = ctx_data or {}
        ticker = ctx_data.get("ticker")
        if not ticker:
            return _error_box("Select a ticker in the Deep Dive tab first.")
        fund = ai_tools.get_fundamentals(ticker)
        hist = dcf_engine.build_historicals_from_fundamentals(fund)
        if hist is None:
            return _error_box(f"Insufficient historical fundamentals for {ticker} to seed a DCF.")

        assumptions = {
            "rev_growth_pct":     [g1 or 0, g2 or 0, g3 or 0, g4 or 0, g5 or 0],
            "terminal_growth_pct": term_g or 2.5,
            "ebit_margin_pct":    ebit_m or 15.0,
            "tax_rate_pct":       tax_r or 21.0,
            "capex_pct_of_rev":   capex or 4.0,
            "nwc_pct_of_rev":     nwc or 2.0,
            "wacc_pct":           wacc or 9.0,
            "share_count_mm":     shares or 0,
        }
        try:
            result = dcf_engine.run(assumptions, hist,
                                     current_price=(px if px and px > 0 else None))
        except Exception as e:
            return _error_box(f"DCF failed: {type(e).__name__}: {e}")

        narrative_text = ""
        api_key = (ui_key or "").strip() or llm_runtime.resolve_env_key()
        if api_key:
            s = settings or {}
            try:
                msg = llm_runtime.chat_once(
                    api_key=api_key,
                    base_url=(s.get("base_url") or llm_runtime.DEFAULT_BASE_URL),
                    model=(s.get("reasoner_model") or llm_runtime.DEFAULT_REASONER),
                    messages=[
                        {"role": "system", "content": DCF_NARRATIVE_PROMPT},
                        {"role": "user", "content": json.dumps({
                            "ticker": ticker,
                            "assumptions": result["assumptions"],
                            "valuation": {
                                "per_share_value": result["per_share_value"],
                                "current_price":   result["current_price"],
                                "upside_pct":      result["upside_pct"],
                                "enterprise_value": result["enterprise_value"],
                                "equity_value":    result["equity_value"],
                            },
                            "sensitivity": result["sensitivity"],
                        }, default=str)},
                    ],
                    temperature=0.3, max_tokens=600,
                )
                narrative_text = (msg.get("content") or "").strip()
            except Exception:
                narrative_text = ""

        return _render_dcf_output(ticker, result, narrative_text)

    # Report: deterministic packet + DeepSeek narrative + archived PDF/HTML/JSON.
    @app.callback(
        Output("ai-report-output", "children"),
        Input("ai-report-run-btn", "n_clicks"),
        State("ai-context-store", "data"),
        State("ai-api-key", "data"),
        State("ai-settings", "data"),
        prevent_initial_call=True,
    )
    def _report_run(_n, ctx_data, ui_key, settings):
        ticker = (ctx_data or {}).get("ticker")
        if not ticker:
            return _error_box("Select a ticker in the Deep Dive tab first.")
        api_key = (ui_key or "").strip() or llm_runtime.resolve_env_key()
        s = settings or {}
        try:
            result = reporting.generate_report(
                ticker,
                api_key=api_key,
                base_url=s.get("base_url") or llm_runtime.DEFAULT_BASE_URL,
                model=s.get("chat_model") or llm_runtime.DEFAULT_CHAT_MODEL,
            )
        except Exception as exc:
            return _error_box(f"Report failed: {type(exc).__name__}: {exc}")
        links = []
        root = reporting.REPORT_ROOT.resolve()
        for label, key in (("PDF", "pdf"), ("HTML", "html"), ("Data packet", "data_packet")):
            path = result.get(key)
            if not path:
                continue
            rel = Path(path).resolve().relative_to(root).as_posix()
            links.append(html.A(label, href=f"/ai-analyst-report/{rel}", target="_blank",
                                style={"color": T.AMBER, "marginRight": "12px", "fontSize": "11px"}))
        return html.Div([
            html.Div(result["message"], style={"fontSize": "12px", "color": T.GREEN, "fontWeight": "700"}),
            html.Div(links, style={"marginTop": "8px"}),
            html.Div(result["output_dir"], style={"fontSize": "10px", "color": T.MUTED,
                                                  "fontFamily": T.MONO, "marginTop": "8px",
                                                  "wordBreak": "break-all"}),
        ])


def _format_trace(trace: list[dict]) -> str:
    lines = []
    for t in trace:
        name = t.get("name", "?")
        args = json.dumps(t.get("arguments", {}), default=str)
        lines.append(f"› {name}({args})")
        preview = t.get("result_preview", "")
        if preview:
            lines.append(f"   ← {preview}")
    return "\n".join(lines)


def _error_box(msg: str) -> html.Div:
    return html.Div(msg, style={"color": T.RED, "background": "#FEF2F2",
                                 "border": f"1px solid #FECACA", "padding": "8px 10px",
                                 "borderRadius": "4px", "fontSize": "11px",
                                 "fontFamily": T.FONT})


def _fmt_money(x: float | None, currency: str = "USD") -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    sign = "-" if x < 0 else ""
    v = abs(x)
    if v >= 1e12: return f"{sign}{currency} {v/1e12:.2f}T"
    if v >= 1e9:  return f"{sign}{currency} {v/1e9:.2f}B"
    if v >= 1e6:  return f"{sign}{currency} {v/1e6:.2f}M"
    return f"{sign}{currency} {v:,.0f}"


def _fmt_pct(x: float | None, decimals: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:+.{decimals}f}%"


def _render_dcf_output(ticker: str, r: dict, narrative: str) -> html.Div:
    currency = "USD"
    a = r["assumptions"]
    kpis = [
        {"label": "Per-share value",
         "value": (f"{r['per_share_value']:.2f}" if r["per_share_value"] == r["per_share_value"] else "—")},
        {"label": "Current price",
         "value": (f"{r['current_price']:.2f}" if r['current_price'] else "—")},
        {"label": "Upside",
         "value": _fmt_pct(r["upside_pct"]) if r["upside_pct"] is not None else "—",
         "delta": _fmt_pct(r["upside_pct"]) if r["upside_pct"] is not None else ""},
        {"label": "Enterprise value", "value": _fmt_money(r["enterprise_value"], currency)},
        {"label": "Equity value",     "value": _fmt_money(r["equity_value"], currency)},
        {"label": "Net debt",         "value": _fmt_money(r["net_debt"], currency)},
        {"label": "WACC",             "value": f"{a['wacc_pct']:.2f}%"},
        {"label": "Terminal g",       "value": f"{a['terminal_growth_pct']:.2f}%"},
    ]

    pi = r["projected_income"]
    income_spec = {
        "type": "table", "title": "Projected Income Statement",
        "columns": ["Year", "Revenue", "EBIT", "Tax", "NOPAT"],
        "rows": [[x["year"], _fmt_money(x["revenue"]), _fmt_money(x["ebit"]),
                  _fmt_money(x["tax"]), _fmt_money(x["nopat"])] for x in pi],
    }

    pc = r["projected_cashflow"]
    cf_spec = {
        "type": "table", "title": "Projected Cash Flow",
        "columns": ["Year", "NOPAT", "D&A", "Capex", "ΔNWC", "FCF"],
        "rows": [[x["year"], _fmt_money(x["nopat"]), _fmt_money(x["d_a"]),
                  _fmt_money(x["capex"]), _fmt_money(x["d_nwc"]), _fmt_money(x["fcf"])]
                 for x in pc],
    }

    pb = r["projected_balance_sheet"]
    bs_spec = {
        "type": "table", "title": "Projected Balance Sheet (selected)",
        "columns": ["Year", "Implied Invested Capital", "Δ Working Capital"],
        "rows": [[x["year"], _fmt_money(x["implied_invested_capital"]),
                  _fmt_money(x["working_capital_addition"])] for x in pb],
    }

    wf = r["waterfall"]
    waterfall_spec = {
        "type": "waterfall", "title": "Value Bridge",
        "x": [x["name"] for x in wf],
        "y": [x["value"] for x in wf],
        "measure": ["relative", "relative", "total", "relative", "total"],
    }

    sens = r["sensitivity"]
    heatmap_spec = {
        "type": "heatmap", "title": "Per-share value: WACC × Terminal growth",
        "x": [f"{g}%" for g in sens["g_axis"]],
        "y": [f"{w}%" for w in sens["wacc_axis"]],
        "z": sens["per_share"],
        "colorscale": "diverging",
    }

    rev_spec = {
        "type": "bar", "title": "Projected Revenue (Y1–Y5)",
        "x": [x["year"] for x in pi],
        "y": [x["revenue"] for x in pi],
        "series_name": "Revenue",
    }

    return html.Div([
        html.Div(f"DCF · {ticker}", style={"fontSize": "12px", "fontWeight": "700",
                                            "color": T.WHITE, "marginBottom": "6px",
                                            "fontFamily": T.FONT, "letterSpacing": "0.04em"}),
        *render_spec.render_visuals([{"type": "kpi_grid", "items": kpis}]),
        (html.Div(narrative, style={"fontSize": "11px", "color": T.WHITE,
                                     "fontFamily": T.FONT, "lineHeight": "1.55",
                                     "marginTop": "8px", "padding": "8px 10px",
                                     "background": T.BORD, "borderRadius": "4px",
                                     "borderLeft": f"3px solid {T.AMBER}"})
         if narrative else None),
        *render_spec.render_visuals([rev_spec, waterfall_spec, heatmap_spec,
                                       income_spec, cf_spec, bs_spec]),
    ])
