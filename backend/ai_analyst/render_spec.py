"""Translate LLM-supplied JSON chart specs into Dash components.

Schema is intentionally narrow. Unknown types degrade to a small "unsupported spec"
chip rather than raising — the LLM occasionally produces weird shapes and we don't
want the panel to crash.
"""
from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
from dash import dcc, html, dash_table

from . import _theme as T


_BASE_LAYOUT = dict(
    paper_bgcolor=T.PANEL,
    plot_bgcolor=T.PANEL,
    font=dict(family=T.FONT, color=T.WHITE, size=11),
    margin=dict(l=42, r=20, t=36, b=36),
    xaxis=dict(gridcolor=T.BORD2, zerolinecolor=T.BORD2, linecolor=T.BORD2, tickcolor=T.BORD2),
    yaxis=dict(gridcolor=T.BORD2, zerolinecolor=T.BORD2, linecolor=T.BORD2, tickcolor=T.BORD2),
    height=300,
    showlegend=True,
    legend=dict(orientation="h", y=-0.18, x=0, font=dict(size=10)),
    hoverlabel=dict(bgcolor=T.PANEL, font_family=T.FONT, font_size=11, bordercolor=T.BORD2),
)


def render_visuals(visuals: list[dict]) -> list[Any]:
    out: list[Any] = []
    for v in visuals or []:
        try:
            comp = _render_one(v)
        except Exception as e:
            comp = _error_chip(f"render error: {e}")
        if comp is not None:
            out.append(html.Div(comp, style={"marginTop": "8px"}))
    return out


def _render_one(spec: dict) -> Any:
    t = (spec.get("type") or "").lower()
    if t == "bar":         return _figure(_make_bar(spec))
    if t == "bar_grouped": return _figure(_make_bar_grouped(spec))
    if t == "line":        return _figure(_make_line(spec))
    if t == "scatter":     return _figure(_make_scatter(spec))
    if t == "waterfall":   return _figure(_make_waterfall(spec))
    if t == "heatmap":     return _figure(_make_heatmap(spec))
    if t == "table":       return _make_table(spec)
    if t == "kpi_grid":    return _make_kpi_grid(spec)
    if t == "markdown":    return dcc.Markdown(spec.get("text", ""), style={"fontFamily": T.FONT, "fontSize": "12px", "color": T.WHITE})
    return _error_chip(f"unsupported type: {t!r}")


def _figure(fig: go.Figure) -> Any:
    return dcc.Graph(figure=fig, config={"displayModeBar": False},
                     style={"border": f"1px solid {T.BORD2}", "borderRadius": "4px"})


def _title(spec: dict) -> str:
    return str(spec.get("title") or "")


def _make_bar(spec: dict) -> go.Figure:
    x = spec.get("x") or []
    y = spec.get("y") or []
    name = spec.get("series_name") or "value"
    fig = go.Figure(go.Bar(x=x, y=y, name=name, marker_color=T.AMBER,
                            hovertemplate="%{x}: %{y:,.2f}<extra></extra>"))
    fig.update_layout(title=_title(spec), **_BASE_LAYOUT)
    return fig


def _make_bar_grouped(spec: dict) -> go.Figure:
    x = spec.get("x") or []
    fig = go.Figure()
    for i, s in enumerate(spec.get("series") or []):
        fig.add_bar(x=x, y=s.get("y") or [], name=str(s.get("name", f"series {i+1}")),
                    marker_color=T.SERIES_COLORS[i % len(T.SERIES_COLORS)])
    fig.update_layout(title=_title(spec), barmode="group", **_BASE_LAYOUT)
    return fig


def _make_line(spec: dict) -> go.Figure:
    x = spec.get("x") or []
    fig = go.Figure()
    for i, s in enumerate(spec.get("series") or []):
        fig.add_scatter(x=x, y=s.get("y") or [], mode="lines+markers",
                        name=str(s.get("name", f"series {i+1}")),
                        line=dict(width=2, color=T.SERIES_COLORS[i % len(T.SERIES_COLORS)]),
                        marker=dict(size=5))
    fig.update_layout(title=_title(spec), **_BASE_LAYOUT)
    return fig


def _make_scatter(spec: dict) -> go.Figure:
    fig = go.Figure(go.Scatter(
        x=spec.get("x") or [], y=spec.get("y") or [], mode="markers",
        marker=dict(color=T.AMBER, size=8, line=dict(color=T.PANEL, width=1)),
        text=spec.get("labels") or None,
    ))
    fig.update_layout(title=_title(spec), **_BASE_LAYOUT)
    return fig


def _make_waterfall(spec: dict) -> go.Figure:
    fig = go.Figure(go.Waterfall(
        x=spec.get("x") or [],
        y=spec.get("y") or [],
        measure=spec.get("measure") or None,
        increasing=dict(marker=dict(color=T.GREEN)),
        decreasing=dict(marker=dict(color=T.RED)),
        totals=dict(marker=dict(color=T.AMBER)),
        connector=dict(line=dict(color=T.MUTED, width=1)),
    ))
    fig.update_layout(title=_title(spec), **_BASE_LAYOUT)
    return fig


def _make_heatmap(spec: dict) -> go.Figure:
    scale = (spec.get("colorscale") or "").lower()
    if scale == "diverging":
        colorscale = [[0, T.RED], [0.5, T.PANEL], [1, T.GREEN]]
    elif scale == "blues":
        colorscale = [[0, T.PANEL], [1, T.AMBER]]
    else:
        colorscale = [[0, T.PANEL], [0.5, T.CYAN], [1, T.AMBER]]
    fig = go.Figure(go.Heatmap(
        z=spec.get("z") or [],
        x=spec.get("x") or None,
        y=spec.get("y") or None,
        colorscale=colorscale,
        zmid=0 if scale == "diverging" else None,
        colorbar=dict(thickness=8, tickfont=dict(size=10, family=T.FONT)),
    ))
    fig.update_layout(title=_title(spec), **_BASE_LAYOUT)
    return fig


def _make_table(spec: dict) -> Any:
    cols = spec.get("columns") or []
    rows = spec.get("rows") or []
    data = [{c: r[i] if i < len(r) else None for i, c in enumerate(cols)} for r in rows]
    return html.Div([
        html.Div(_title(spec), style={"fontSize": "11px", "fontWeight": 600, "color": T.MUTED,
                                      "marginBottom": "4px", "fontFamily": T.FONT,
                                      "textTransform": "uppercase"}) if _title(spec) else None,
        dash_table.DataTable(
            data=data,
            columns=[{"name": c, "id": c} for c in cols],
            style_as_list_view=True,
            style_cell={"fontFamily": T.MONO, "fontSize": "11px", "padding": "4px 8px",
                         "textAlign": "right", "border": f"1px solid {T.BORD2}"},
            style_header={"backgroundColor": T.BORD, "color": T.MUTED, "fontWeight": "600",
                           "fontFamily": T.FONT, "fontSize": "10px", "textTransform": "uppercase",
                           "textAlign": "right"},
            style_data={"backgroundColor": T.PANEL, "color": T.WHITE},
            page_size=12,
        ),
    ])


def _make_kpi_grid(spec: dict) -> Any:
    items = spec.get("items") or []
    cards = []
    for it in items:
        delta = str(it.get("delta", "") or "")
        delta_color = T.MUTED
        if delta.startswith("+"): delta_color = T.GREEN
        elif delta.startswith("-"): delta_color = T.RED
        cards.append(html.Div([
            html.Div(str(it.get("label", "")), style={"fontSize": "10px", "color": T.MUTED,
                                                       "fontFamily": T.FONT, "textTransform": "uppercase",
                                                       "fontWeight": 600, "letterSpacing": "0.05em"}),
            html.Div(str(it.get("value", "")), style={"fontSize": "18px", "fontFamily": T.MONO,
                                                       "color": T.WHITE, "fontWeight": 600,
                                                       "marginTop": "2px"}),
            html.Div(delta, style={"fontSize": "10px", "color": delta_color, "fontFamily": T.MONO,
                                    "marginTop": "2px"}) if delta else None,
        ], style={"background": T.PANEL, "border": f"1px solid {T.BORD2}", "borderRadius": "4px",
                   "padding": "8px 10px", "minWidth": "100px", "flex": "1 1 100px"}))
    return html.Div(cards, style={"display": "flex", "gap": "8px", "flexWrap": "wrap"})


def _error_chip(text: str) -> Any:
    return html.Div(text, style={"fontSize": "10px", "color": T.RED, "fontFamily": T.MONO,
                                  "padding": "4px 8px", "background": T.BORD,
                                  "borderRadius": "3px", "display": "inline-block"})
