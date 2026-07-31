"""Floating overlay layout for the AI Analyst panel."""
from __future__ import annotations

from dash import dcc, html
import dash_bootstrap_components as dbc

from . import _theme as T
from . import llm_runtime


PILL_LABEL  = "AI ANALYST"
HEADER_BG   = "linear-gradient(90deg, #1A2744 0%, #3B465C 100%)"

QUICK_PROMPTS = [
    "5-yr revenue trend",
    "Margin trajectory",
    "Compare vs sector peers",
    "Build a DCF",
]


def _icon_btn(id_: str, label: str, title: str) -> html.Button:
    return html.Button(
        label, id=id_, n_clicks=0, title=title,
        style={
            "background": "transparent", "border": "none", "color": "rgba(255,255,255,0.85)",
            "fontSize": "14px", "padding": "0 8px", "cursor": "pointer", "fontFamily": T.MONO,
            "lineHeight": "1",
        },
    )


def _tab_btn(id_: str, label: str, active: bool = False) -> html.Button:
    cls = "ai-tab-btn active" if active else "ai-tab-btn"
    return html.Button(label, id=id_, n_clicks=0, className=cls)


def _section_label(text: str) -> html.Div:
    return html.Div(text, style={
        "fontSize": "10px", "color": T.MUTED, "fontWeight": "600",
        "letterSpacing": "0.06em", "textTransform": "uppercase", "marginBottom": "4px",
        "fontFamily": T.FONT,
    })


def _dcf_field(label: str, id_: str, value, step: float = 0.1) -> html.Div:
    return html.Div([
        _section_label(label),
        dcc.Input(id=id_, type="number", value=value, step=step, debounce=True,
                  className="ai-dcf-input"),
    ], style={"flex": "1 1 80px", "minWidth": "70px"})


def _settings_view() -> html.Div:
    has_env_key = bool(llm_runtime.resolve_env_key())
    status_text = "Key loaded from environment" if has_env_key else "No environment key — paste one below"
    status_color = T.GREEN if has_env_key else T.YELLOW
    return html.Div([
        html.Div([
            _section_label("DeepSeek API Key (session-only)"),
            dcc.Input(id="ai-settings-key", type="password", placeholder="sk-…",
                       debounce=True, className="ai-dcf-input",
                       autoComplete="off"),
            html.Div([
                html.Span("●", style={"color": status_color, "marginRight": "6px"}),
                html.Span(status_text, id="ai-settings-status",
                          style={"fontSize": "10px", "color": T.MUTED, "fontFamily": T.MONO}),
            ], style={"marginTop": "4px"}),
            html.Div(
                "Stored only in your browser session (sessionStorage). Cleared when you close the tab.",
                style={"fontSize": "10px", "color": T.MUTED, "marginTop": "4px",
                       "fontStyle": "italic"},
            ),
        ], style={"marginBottom": "12px"}),

        html.Div([
            html.Div([
                _section_label("Chat model"),
                dcc.Input(id="ai-settings-chat-model", type="text",
                           value=llm_runtime.DEFAULT_CHAT_MODEL,
                           debounce=True, className="ai-dcf-input"),
            ], style={"flex": "1 1 50%"}),
            html.Div([
                _section_label("Reasoner model (DCF)"),
                dcc.Input(id="ai-settings-reasoner-model", type="text",
                           value=llm_runtime.DEFAULT_REASONER,
                           debounce=True, className="ai-dcf-input"),
            ], style={"flex": "1 1 50%"}),
        ], style={"display": "flex", "gap": "8px", "marginBottom": "12px"}),

        html.Div([
            html.Div([
                _section_label("Temperature"),
                dcc.Input(id="ai-settings-temperature", type="number",
                           value=0.2, step=0.05, min=0, max=1.5,
                           debounce=True, className="ai-dcf-input"),
            ], style={"flex": "1 1 50%"}),
            html.Div([
                _section_label("Max tokens"),
                dcc.Input(id="ai-settings-max-tokens", type="number",
                           value=2000, step=100, min=200, max=8000,
                           debounce=True, className="ai-dcf-input"),
            ], style={"flex": "1 1 50%"}),
        ], style={"display": "flex", "gap": "8px", "marginBottom": "12px"}),

        html.Div([
            _section_label("Base URL"),
            dcc.Input(id="ai-settings-base-url", type="text",
                       value=llm_runtime.DEFAULT_BASE_URL,
                       debounce=True, className="ai-dcf-input"),
        ], style={"marginBottom": "12px"}),

        html.Div([
            html.Button("Reset conversation", id="ai-settings-reset", n_clicks=0,
                         style={"background": T.PANEL, "border": f"1px solid {T.BORD2}",
                                "color": T.WHITE, "padding": "5px 10px", "fontSize": "11px",
                                "borderRadius": "4px", "cursor": "pointer",
                                "fontFamily": T.FONT}),
            html.Span(id="ai-settings-reset-flash", style={"marginLeft": "10px",
                                                            "fontSize": "10px",
                                                            "color": T.GREEN}),
        ]),
    ], style={"padding": "12px"})


def _dcf_view() -> html.Div:
    return html.Div([
        html.Div([
            _section_label("Revenue growth (% YoY, Y1–Y5)"),
            html.Div([
                _dcf_field("Y1", "ai-dcf-g1", 8.0, 0.5),
                _dcf_field("Y2", "ai-dcf-g2", 7.0, 0.5),
                _dcf_field("Y3", "ai-dcf-g3", 6.0, 0.5),
                _dcf_field("Y4", "ai-dcf-g4", 5.0, 0.5),
                _dcf_field("Y5", "ai-dcf-g5", 4.0, 0.5),
            ], style={"display": "flex", "gap": "6px"}),
        ], style={"marginBottom": "10px"}),

        html.Div([
            _dcf_field("Terminal g (%)", "ai-dcf-term-g", 2.5, 0.1),
            _dcf_field("EBIT margin (%)", "ai-dcf-ebit-margin", 22.0, 0.5),
            _dcf_field("Tax rate (%)", "ai-dcf-tax-rate", 21.0, 0.5),
        ], style={"display": "flex", "gap": "6px", "marginBottom": "8px"}),

        html.Div([
            _dcf_field("Capex / Rev (%)", "ai-dcf-capex", 4.0, 0.25),
            _dcf_field("ΔNWC / Rev (%)", "ai-dcf-nwc", 2.0, 0.25),
            _dcf_field("WACC (%)", "ai-dcf-wacc", 9.0, 0.25),
        ], style={"display": "flex", "gap": "6px", "marginBottom": "8px"}),

        html.Div([
            _dcf_field("Shares (mm)", "ai-dcf-shares", 0.0, 1.0),
            _dcf_field("Current px", "ai-dcf-px", 0.0, 0.5),
        ], style={"display": "flex", "gap": "6px", "marginBottom": "10px"}),

        html.Div([
            html.Button("✶ Ask AI for assumptions", id="ai-dcf-ask-btn", n_clicks=0,
                         style={"background": T.PANEL, "border": f"1px solid {T.AMBER}",
                                "color": T.AMBER, "padding": "6px 12px", "fontSize": "11px",
                                "fontWeight": "600", "borderRadius": "4px", "cursor": "pointer",
                                "fontFamily": T.FONT, "marginRight": "6px"}),
            html.Button("Run DCF", id="ai-dcf-run-btn", n_clicks=0,
                         style={"background": T.AMBER, "border": "none", "color": "white",
                                "padding": "6px 14px", "fontSize": "11px", "fontWeight": "600",
                                "borderRadius": "4px", "cursor": "pointer", "fontFamily": T.FONT}),
        ], style={"marginBottom": "10px"}),

        dcc.Loading(type="default", color=T.AMBER, children=html.Div(id="ai-dcf-output")),
    ], style={"padding": "12px", "overflowY": "auto", "flex": "1 1 auto"})


def _report_view() -> html.Div:
    return html.Div([
        html.Div([
            _section_label("Analyst report"),
            html.Div(
                "Generates a compact two-page note from modeled statements, metrics, market data, peers, factor exposure, and corporate DCF.",
                style={"fontSize": "11px", "color": T.MUTED, "lineHeight": "1.4", "marginBottom": "10px"},
            ),
            html.Button("Generate Report", id="ai-report-run-btn", n_clicks=0,
                         style={"background": T.AMBER, "border": "none", "color": "white",
                                "padding": "7px 14px", "fontSize": "11px", "fontWeight": "600",
                                "borderRadius": "4px", "cursor": "pointer", "fontFamily": T.FONT}),
        ], style={"marginBottom": "12px"}),
        dcc.Loading(type="default", color=T.AMBER, children=html.Div(id="ai-report-output")),
    ], style={"padding": "12px", "overflowY": "auto", "flex": "1 1 auto"})


def _chat_view() -> html.Div:
    return html.Div([
        html.Div(id="ai-chat-messages",
                  style={"flex": "1 1 auto", "overflowY": "auto", "padding": "12px",
                         "display": "flex", "flexDirection": "column", "gap": "8px"}),

        html.Div(id="ai-chat-thinking",
                  style={"padding": "0 12px 4px 12px", "fontSize": "10px",
                          "color": T.MUTED, "fontFamily": T.MONO, "minHeight": "14px"}),

        html.Div([
            dcc.Textarea(id="ai-chat-input",
                          placeholder="Ask anything about company fundamentals…  (Shift+Enter for newline)",
                          style={"width": "100%", "border": f"1px solid {T.BORD2}",
                                 "borderRadius": "4px", "padding": "6px 8px",
                                 "fontFamily": T.FONT, "fontSize": "12px",
                                 "minHeight": "44px", "maxHeight": "120px"}),
            html.Div([
                html.Button("Send", id="ai-chat-send", n_clicks=0,
                             style={"background": T.AMBER, "border": "none", "color": "white",
                                    "padding": "6px 16px", "fontSize": "11px", "fontWeight": "600",
                                    "borderRadius": "4px", "cursor": "pointer", "fontFamily": T.FONT,
                                    "marginRight": "6px"}),
                html.Button("Clear", id="ai-chat-clear", n_clicks=0,
                             style={"background": "transparent", "border": f"1px solid {T.BORD2}",
                                    "color": T.MUTED, "padding": "6px 12px", "fontSize": "11px",
                                    "borderRadius": "4px", "cursor": "pointer", "fontFamily": T.FONT}),
            ], style={"display": "flex", "alignItems": "center", "marginTop": "6px"}),
        ], style={"padding": "8px 12px 4px 12px", "borderTop": f"1px solid {T.BORD2}",
                   "background": T.PANEL}),

        html.Div([
            html.Span("Try:", style={"fontSize": "10px", "color": T.MUTED,
                                       "marginRight": "6px", "fontFamily": T.FONT}),
            *[html.Button(p, id={"type": "ai-quick-prompt", "index": i}, n_clicks=0,
                          style={"background": T.BORD, "border": f"1px solid {T.BORD2}",
                                 "color": T.WHITE, "padding": "3px 8px", "fontSize": "10px",
                                 "borderRadius": "11px", "cursor": "pointer", "marginRight": "4px",
                                 "fontFamily": T.FONT})
                for i, p in enumerate(QUICK_PROMPTS)],
        ], style={"padding": "0 12px 10px 12px", "background": T.PANEL,
                   "display": "flex", "alignItems": "center", "flexWrap": "wrap"}),
    ], style={"display": "flex", "flexDirection": "column", "flex": "1 1 auto",
               "minHeight": "0"})


def build_ai_analyst_panel() -> html.Div:
    return html.Div(id="ai-panel-root", children=[
        dcc.Store(id="ai-api-key",       storage_type="session"),
        dcc.Store(id="ai-settings",      storage_type="session"),
        dcc.Store(id="ai-chat-history",  storage_type="memory", data=[]),
        dcc.Store(id="ai-chat-bubbles",  storage_type="memory", data=[]),
        dcc.Store(id="ai-ui-state",      storage_type="local", data={"open": False, "tab": "chat"}),
        dcc.Store(id="ai-context-store", storage_type="memory", data={"ticker": None, "jurisdiction": "US"}),
        dcc.Store(id="ai-dcf-trigger",   storage_type="memory", data={}),
        dcc.Store(id="ai-pending-msg",   storage_type="memory", data=None),

        html.Button([
            html.Span("✨ ", style={"marginRight": "4px"}),
            PILL_LABEL,
        ], id="ai-panel-pill", n_clicks=0),

        html.Div(id="ai-panel-card", style={"display": "none"}, children=[
            html.Div(id="ai-header-drag", children=[
                html.Div([
                    html.Span("AI ANALYST", style={"fontWeight": "700", "letterSpacing": "0.08em",
                                                    "fontSize": "11px"}),
                    html.Span(id="ai-context-chip",
                              style={"fontSize": "10px", "color": "rgba(255,255,255,0.75)",
                                     "marginLeft": "10px", "fontFamily": T.MONO}),
                ], style={"flex": "1 1 auto", "color": "white"}),
                html.Div([
                    _icon_btn("ai-min-btn", "—", "Minimise"),
                    _icon_btn("ai-close-btn", "✕", "Close"),
                ], style={"display": "flex"}),
            ], style={"display": "flex", "alignItems": "center", "padding": "8px 12px",
                       "background": HEADER_BG, "color": "white"}),

            html.Div([
                _tab_btn("ai-tab-chat",     "Chat",     active=True),
                _tab_btn("ai-tab-dcf",      "DCF Builder"),
                _tab_btn("ai-tab-report",   "Report"),
                _tab_btn("ai-tab-settings", "Settings"),
            ], style={"display": "flex", "borderBottom": f"1px solid {T.BORD2}",
                       "background": T.BORD}),

            html.Div(id="ai-view-chat",     children=_chat_view(),
                     style={"display": "flex", "flex": "1 1 auto", "minHeight": "0",
                             "flexDirection": "column"}),
            html.Div(id="ai-view-dcf",      children=_dcf_view(),     style={"display": "none"}),
            html.Div(id="ai-view-report",   children=_report_view(),  style={"display": "none"}),
            html.Div(id="ai-view-settings", children=_settings_view(), style={"display": "none"}),
        ]),
    ])
