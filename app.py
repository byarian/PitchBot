"""PitchBot — MLB Ball/Strike Accuracy Tracker."""

import json
import os
from datetime import date, timedelta

import dash
import dash_bootstrap_components as dbc
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html

from database import (
    get_filter_options,
    get_first_game_date,
    get_last_game_date,
    get_pitch_count,
    init_db,
    query_by_group,
    query_by_pitch_type_hand,
    query_pitches,
    query_summary_stats,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRIKE_ZONE_LEFT = -0.83
STRIKE_ZONE_RIGHT = 0.83
TYPICAL_SZ_TOP = 3.5
TYPICAL_SZ_BOT = 1.5

PITCH_NAMES = {
    "FF": "4-Seam FB", "FA": "Fastball", "SI": "Sinker",
    "FC": "Cutter", "SL": "Slider", "SW": "Sweeper",
    "SV": "Sweeper", "ST": "Swp. Curve", "CH": "Changeup",
    "CU": "Curveball", "KC": "Knuckle Curve", "FS": "Splitter",
    "KN": "Knuckleball", "EP": "Eephus", "FO": "Forkball",
    "SC": "Screwball", "CS": "Slow Curve", "PO": "Pitchout",
    "IN": "Int. Ball",
}

CAT_COLORS = {
    "correct_strike": "#2ecc71",
    "correct_ball":   "#3498db",
    "phantom_strike": "#e74c3c",
    "missed_strike":  "#f39c12",
    "abs_overturned": "#9b59b6",
}

CAT_SYMBOLS = {
    "correct_strike": "circle",
    "correct_ball":   "circle-open",
    "phantom_strike": "x-thin",
    "missed_strike":  "diamond-open",
    "abs_overturned": "star",
}

CAT_LABELS = {
    "correct_strike": "Correct Strike",
    "correct_ball":   "Correct Ball",
    "phantom_strike": "Phantom Strike (bad call)",
    "missed_strike":  "Missed Strike (bad call)",
    "abs_overturned": "ABS Overturned",
}

PLOT_BG = "#0d1117"
PLOT_PAPER = "#0d1117"
AXIS_COLOR = "#30363d"
TEXT_COLOR = "#8b949e"


# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

init_db()

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG, dbc.icons.FONT_AWESOME],
    title="PitchBot",
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    suppress_callback_exceptions=True,
)
server = app.server


# ---------------------------------------------------------------------------
# Helper: build strike zone figure
# ---------------------------------------------------------------------------

def _compute_zone_distance(plate_x, plate_z, sz_top, sz_bot):
    """Return (h_dist_in, v_dist_in, side_str) where positive = inside zone."""
    try:
        px, pz = float(plate_x), float(plate_z)
        st, sb = float(sz_top or TYPICAL_SZ_TOP), float(sz_bot or TYPICAL_SZ_BOT)
    except (TypeError, ValueError):
        return None, None, ""
    h_margin = (STRIKE_ZONE_RIGHT - abs(px)) * 12      # positive = inside
    if pz > st:
        v_margin = -(pz - st) * 12                     # above zone
        v_side = "above"
    elif pz < sb:
        v_margin = -(sb - pz) * 12                     # below zone
        v_side = "below"
    else:
        v_margin = min((st - pz), (pz - sb)) * 12      # inside zone
        v_side = "inside"
    return h_margin, v_margin, v_side


def _hover_text(r):
    pitcher = f"{r.get('pitcher_name') or '?'} ({'RHP' if r.get('p_throws') == 'R' else 'LHP' if r.get('p_throws') == 'L' else '?'})"
    bname = r.get("batter_name") or f"ID {r.get('batter_id', '?')}"
    batter = f"{bname} ({'RHB' if r.get('stand') == 'R' else 'LHB' if r.get('stand') == 'L' else '?'})"
    pt = PITCH_NAMES.get(r.get("pitch_type", ""), r.get("pitch_type") or "?")
    spd = f"{r['release_speed']:.1f} mph" if r.get("release_speed") else "? mph"
    called = "Called STRIKE" if r.get("description") == "called_strike" else "Called BALL"
    if r.get("correct_call") == 1:
        verdict = "✓ Correct"
    elif r.get("correct_call") == 0:
        if r.get("description") == "called_strike":
            verdict = "✗ Phantom Strike (should be BALL)"
        else:
            verdict = "✗ Missed Strike (should be STRIKE)"
    else:
        verdict = "—"
    h_d, v_d, v_side = _compute_zone_distance(
        r.get("plate_x"), r.get("plate_z"), r.get("sz_top"), r.get("sz_bot")
    )
    if h_d is not None:
        h_str = f"{abs(h_d):.1f}\" {'inside' if h_d > 0 else 'outside'} (H)"
    else:
        h_str = "?"
    if v_d is not None:
        v_str = f"{abs(v_d):.1f}\" {v_side} (V)"
    else:
        v_str = "?"
    ump = r.get("umpire") or "?"
    gdate = r.get("game_date", "?")
    teams = f"{r.get('away_team','?')} @ {r.get('home_team','?')}"
    abs_str = f"ABS: {r['abs_result'].title()}" if r.get("abs_result") else "ABS: Not challenged"
    return (
        f"<b>{pitcher}</b> vs {batter}<br>"
        f"Umpire: {ump} | {gdate} | {teams}<br>"
        f"<br><b>{called}</b>  {verdict}<br>"
        f"<br>Pitch: {pt} @ {spd}<br>"
        f"Zone edge: {h_str} | {v_str}<br>"
        f"{abs_str}"
    )


def _categorize(r):
    if r.get("abs_result") == "overturned":
        return "abs_overturned"
    if r.get("correct_call") == 1:
        return "correct_strike" if r.get("description") == "called_strike" else "correct_ball"
    if r.get("correct_call") == 0:
        return "phantom_strike" if r.get("description") == "called_strike" else "missed_strike"
    return None


def build_scatter_figure(pitches, total_count):
    """Build the strike zone scatter plot."""
    fig = go.Figure()

    # Compute average zone for this filter
    szs = [(r.get("sz_top"), r.get("sz_bot")) for r in pitches if r.get("sz_top") and r.get("sz_bot")]
    if szs:
        sz_top = float(np.median([s[0] for s in szs]))
        sz_bot = float(np.median([s[1] for s in szs]))
    else:
        sz_top, sz_bot = TYPICAL_SZ_TOP, TYPICAL_SZ_BOT

    # Zone height thirds
    h_step = (sz_top - sz_bot) / 3

    # Draw zone 9-cell grid
    for yi in [sz_bot + h_step, sz_bot + 2 * h_step]:
        fig.add_shape(type="line", x0=STRIKE_ZONE_LEFT, x1=STRIKE_ZONE_RIGHT,
                      y0=yi, y1=yi, line=dict(color="rgba(200,200,200,0.25)", width=1, dash="dot"))
    for xi in [-0.83 + (1.66 / 3), -0.83 + 2 * (1.66 / 3)]:
        fig.add_shape(type="line", x0=xi, x1=xi, y0=sz_bot, y1=sz_top,
                      line=dict(color="rgba(200,200,200,0.25)", width=1, dash="dot"))

    # Outer strike zone rectangle
    fig.add_shape(
        type="rect",
        x0=STRIKE_ZONE_LEFT, x1=STRIKE_ZONE_RIGHT, y0=sz_bot, y1=sz_top,
        line=dict(color="white", width=2),
        fillcolor="rgba(255,255,255,0.03)",
    )

    # Home plate indicator (pentagon approximation)
    plate_w = 0.708  # actual plate half-width
    fig.add_shape(type="line", x0=-plate_w, x1=plate_w, y0=0.25, y1=0.25,
                  line=dict(color="rgba(255,255,255,0.4)", width=1.5))
    fig.add_shape(type="line", x0=-plate_w, x1=-plate_w, y0=0.25, y1=0.42,
                  line=dict(color="rgba(255,255,255,0.4)", width=1.5))
    fig.add_shape(type="line", x0=plate_w, x1=plate_w, y0=0.25, y1=0.42,
                  line=dict(color="rgba(255,255,255,0.4)", width=1.5))
    fig.add_shape(type="line", x0=-plate_w, x1=0, y0=0.42, y1=0.56,
                  line=dict(color="rgba(255,255,255,0.4)", width=1.5))
    fig.add_shape(type="line", x0=plate_w, x1=0, y0=0.42, y1=0.56,
                  line=dict(color="rgba(255,255,255,0.4)", width=1.5))

    # Group pitches by category
    cats = {c: {"x": [], "y": [], "text": []} for c in CAT_COLORS}
    for r in pitches:
        cat = _categorize(r)
        if cat and r.get("plate_x") is not None and r.get("plate_z") is not None:
            cats[cat]["x"].append(r["plate_x"])
            cats[cat]["y"].append(r["plate_z"])
            cats[cat]["text"].append(_hover_text(r))

    # Add traces in order (incorrect on top)
    order = ["correct_ball", "correct_strike", "missed_strike", "phantom_strike", "abs_overturned"]
    for cat in order:
        d = cats[cat]
        if not d["x"]:
            continue
        is_incorrect = cat in ("phantom_strike", "missed_strike", "abs_overturned")
        fig.add_trace(go.Scattergl(
            x=d["x"], y=d["y"],
            mode="markers",
            name=CAT_LABELS[cat],
            marker=dict(
                color=CAT_COLORS[cat],
                symbol=CAT_SYMBOLS[cat],
                size=7 if is_incorrect else 5,
                opacity=0.85 if is_incorrect else 0.55,
                line=dict(width=1, color=CAT_COLORS[cat]),
            ),
            text=d["text"],
            hovertemplate="%{text}<extra></extra>",
        ))

    shown = len(pitches)
    title_note = ""
    if total_count > shown:
        title_note = f" (showing {shown:,} of {total_count:,})"

    fig.update_layout(
        paper_bgcolor=PLOT_PAPER,
        plot_bgcolor=PLOT_BG,
        font=dict(color=TEXT_COLOR, size=11),
        margin=dict(l=50, r=20, t=40, b=50),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=-0.18,
            xanchor="center", x=0.5,
            font=dict(size=10),
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(
            title="Horizontal Position (ft from plate center)",
            range=[-2.5, 2.5],
            zeroline=True, zerolinecolor=AXIS_COLOR, zerolinewidth=1,
            gridcolor=AXIS_COLOR, showgrid=True,
            tickfont=dict(size=10),
            title_font=dict(size=11),
        ),
        yaxis=dict(
            title="Height (ft from ground)",
            range=[0.2, 5.2],
            zeroline=False,
            gridcolor=AXIS_COLOR, showgrid=True,
            tickfont=dict(size=10),
            title_font=dict(size=11),
            scaleanchor="x", scaleratio=1,
        ),
        title=dict(
            text=f"Strike Zone Plot{title_note}",
            font=dict(size=13, color="#e6edf3"),
            x=0.5, xanchor="center",
        ),
        hovermode="closest",
        dragmode="pan",
        height=580,
    )
    return fig


def build_density_figure(pitches, total_count):
    """Build a miss-rate heatmap over the zone grid."""
    if not pitches:
        return _empty_fig("No data for density map.")

    df = pd.DataFrame(pitches)
    df = df.dropna(subset=["plate_x", "plate_z", "correct_call"])

    if df.empty:
        return _empty_fig("No called pitches to map.")

    xbins = np.linspace(-2, 2, 25)
    ybins = np.linspace(0.5, 5.5, 25)
    dx = xbins[1] - xbins[0]
    dy = ybins[1] - ybins[0]

    total_grid = np.zeros((len(ybins) - 1, len(xbins) - 1))
    wrong_grid = np.zeros_like(total_grid)

    for _, r in df.iterrows():
        xi = int((r["plate_x"] - xbins[0]) / dx)
        yi = int((r["plate_z"] - ybins[0]) / dy)
        if 0 <= xi < total_grid.shape[1] and 0 <= yi < total_grid.shape[0]:
            total_grid[yi, xi] += 1
            if r["correct_call"] == 0:
                wrong_grid[yi, xi] += 1

    miss_rate = np.where(total_grid >= 5, wrong_grid / total_grid * 100, np.nan)

    szs = [(r.get("sz_top"), r.get("sz_bot")) for r in pitches if r.get("sz_top") and r.get("sz_bot")]
    sz_top = float(np.median([s[0] for s in szs])) if szs else TYPICAL_SZ_TOP
    sz_bot = float(np.median([s[1] for s in szs])) if szs else TYPICAL_SZ_BOT

    xcen = (xbins[:-1] + xbins[1:]) / 2
    ycen = (ybins[:-1] + ybins[1:]) / 2

    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        x=xcen, y=ycen, z=miss_rate,
        colorscale=[[0, "#2ecc71"], [0.1, "#f9c74f"], [0.25, "#f39c12"], [1.0, "#e74c3c"]],
        zmin=0, zmax=25,
        colorbar=dict(title="Miss %", thickness=12, len=0.7, tickfont=dict(size=10)),
        hovertemplate="x=%{x:.2f}ft, z=%{y:.2f}ft<br>Miss rate: %{z:.1f}%<extra></extra>",
    ))

    fig.add_shape(type="rect", x0=STRIKE_ZONE_LEFT, x1=STRIKE_ZONE_RIGHT,
                  y0=sz_bot, y1=sz_top, line=dict(color="white", width=2))

    fig.update_layout(
        paper_bgcolor=PLOT_PAPER, plot_bgcolor=PLOT_BG,
        font=dict(color=TEXT_COLOR, size=11),
        margin=dict(l=50, r=20, t=40, b=50),
        xaxis=dict(title="Horizontal Position (ft)", range=[-2.5, 2.5],
                   gridcolor=AXIS_COLOR, tickfont=dict(size=10)),
        yaxis=dict(title="Height (ft)", range=[0.2, 5.2], gridcolor=AXIS_COLOR,
                   tickfont=dict(size=10), scaleanchor="x", scaleratio=1),
        title=dict(text=f"Miss Rate Density  (≥5 pitches/cell, n={len(df):,})",
                   font=dict(size=13, color="#e6edf3"), x=0.5, xanchor="center"),
        height=580,
    )
    return fig


def _empty_fig(msg="No data available."):
    fig = go.Figure()
    fig.add_annotation(text=msg, x=0.5, y=0.5, xref="paper", yref="paper",
                       showarrow=False, font=dict(size=14, color=TEXT_COLOR))
    fig.update_layout(paper_bgcolor=PLOT_PAPER, plot_bgcolor=PLOT_BG,
                      margin=dict(l=50, r=20, t=40, b=50), height=580,
                      xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _kpi_card(card_id, label, color_class):
    return dbc.Col(
        html.Div([
            html.Div("—", id=f"kpi-{card_id}", className=f"kpi-value kpi-{color_class}"),
            html.Div(label, className="kpi-label"),
        ], className="kpi-card"),
        xs=6, sm=4, md=True,
    )


def _make_table(cols, table_id):
    return dash_table.DataTable(
        id=table_id,
        columns=cols,
        data=[],
        page_size=12,
        style_table={"overflowX": "auto"},
        style_cell={"backgroundColor": "#1c2333", "color": "#e6edf3",
                    "border": "1px solid #30363d", "fontSize": "12px",
                    "textAlign": "left", "padding": "6px 10px"},
        style_header={"backgroundColor": "#0d1117", "fontWeight": "bold",
                      "color": "#8b949e", "border": "1px solid #30363d"},
        style_data_conditional=[
            {"if": {"column_id": "miss_rate"},
             "background": "linear-gradient(90deg,transparent,rgba(231,76,60,0.25))",
             "color": "#f39c12"},
            {"if": {"row_index": "odd"}, "backgroundColor": "rgba(255,255,255,0.02)"},
        ],
        sort_action="native",
        filter_action="native",
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _today_str():
    return str(date.today())


def _season_start():
    return "2025-03-20"


def build_layout():
    last = get_last_game_date()
    first = get_first_game_date() or _season_start()
    pitch_ct = get_pitch_count()
    opts = get_filter_options()

    last_updated = f"Last data: {last}" if last else "No data loaded — run: python update.py --full-season"
    status_color = "#2ecc71" if last else "#e74c3c"

    return dbc.Container([
        # ── Navbar ──────────────────────────────────────────────────────────
        dbc.Navbar(
            dbc.Container([
                html.A(
                    dbc.Row([
                        dbc.Col(html.Img(src="/assets/icon.svg", height="36px")),
                        dbc.Col(html.Span([
                            html.Span("Pitch", className="text-white fw-bold"),
                            html.Span("Bot", style={"color": "#2ecc71", "fontWeight": "700"}),
                        ], style={"fontSize": "1.4rem", "letterSpacing": "1px"})),
                    ], align="center", className="g-2"),
                    href="/", style={"textDecoration": "none"},
                ),
                dbc.NavbarToggler(id="navbar-toggler"),
                html.Span(last_updated, style={
                    "fontSize": "0.72rem", "color": status_color,
                    "marginLeft": "auto", "padding": "0.2rem 0.7rem",
                    "background": "rgba(255,255,255,0.05)",
                    "borderRadius": "12px", "border": f"1px solid {status_color}33",
                }),
            ], fluid=True),
            color="dark", dark=True,
            style={"background": "linear-gradient(90deg,#0d1117,#0f3460)", "borderBottom": "1px solid #30363d"},
            className="mb-3",
        ),

        # No-data banner
        dbc.Alert(
            [html.I(className="fa fa-database me-2"),
             "No pitch data loaded. Run: ",
             html.Code("python update.py --full-season"),
             " to fetch the 2025 season, then refresh."],
            id="no-data-alert",
            color="warning",
            dismissable=True,
            is_open=pitch_ct == 0,
            className="mb-3",
        ),

        # ── Filter panel ────────────────────────────────────────────────────
        dbc.Card([
            dbc.CardHeader(
                dbc.Row([
                    dbc.Col(html.Span([
                        html.I(className="fa fa-filter me-2", style={"color": "#3498db"}),
                        html.Span("Filters", className="fw-semibold"),
                    ])),
                    dbc.Col(
                        dbc.ButtonGroup([
                            dbc.Button("Apply", id="btn-apply", color="primary", size="sm", n_clicks=0,
                                       className="btn-apply"),
                            dbc.Button("Reset", id="btn-reset", color="secondary", size="sm", n_clicks=0,
                                       className="btn-reset"),
                        ], size="sm"),
                        width="auto",
                    ),
                ], align="center"),
                className="py-2",
            ),
            dbc.CardBody([
                # Row 1: Date, Umpire, Team, Pitcher, Batter
                dbc.Row([
                    dbc.Col([
                        html.Div("Date Range", className="filter-label"),
                        dcc.DatePickerRange(
                            id="date-picker",
                            start_date=first,
                            end_date=last or _today_str(),
                            display_format="MMM D, YYYY",
                            style={"width": "100%"},
                        ),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Div("Umpire", className="filter-label"),
                        dcc.Dropdown(id="filter-umpire", options=opts["umpires"],
                                     multi=True, placeholder="All umpires",
                                     style={"fontSize": "12px"}),
                    ], xs=12, md=2),
                    dbc.Col([
                        html.Div("Team", className="filter-label"),
                        dcc.Dropdown(id="filter-team", options=opts["teams"],
                                     multi=True, placeholder="All teams",
                                     style={"fontSize": "12px"}),
                    ], xs=12, md=2),
                    dbc.Col([
                        html.Div("Pitcher", className="filter-label"),
                        dcc.Dropdown(id="filter-pitcher", options=opts["pitchers"],
                                     multi=True, placeholder="All pitchers",
                                     style={"fontSize": "12px"}),
                    ], xs=12, md=2),
                    dbc.Col([
                        html.Div("Batter", className="filter-label"),
                        dcc.Dropdown(id="filter-batter", options=opts["batters"],
                                     multi=True, placeholder="All batters",
                                     style={"fontSize": "12px"}),
                    ], xs=12, md=3),
                ], className="mb-2 gy-2"),
                # Row 2: Pitch type, Throwing hand, Call type, ABS
                dbc.Row([
                    dbc.Col([
                        html.Div("Pitch Type", className="filter-label"),
                        dcc.Dropdown(id="filter-pitch-type", options=opts["pitch_types"],
                                     multi=True, placeholder="All pitch types",
                                     style={"fontSize": "12px"}),
                    ], xs=12, md=4),
                    dbc.Col([
                        html.Div("Throwing Hand", className="filter-label"),
                        dbc.RadioItems(
                            id="filter-p-throws",
                            options=[
                                {"label": "Both", "value": "B"},
                                {"label": "RHP", "value": "R"},
                                {"label": "LHP", "value": "L"},
                            ],
                            value="B",
                            inline=True,
                            className="mt-1",
                            inputStyle={"marginRight": "4px"},
                            labelStyle={"fontSize": "12px", "marginRight": "12px"},
                        ),
                    ], xs=12, md=2),
                    dbc.Col([
                        html.Div("Call Correctness", className="filter-label"),
                        dbc.RadioItems(
                            id="filter-call-type",
                            options=[
                                {"label": "All called", "value": "all"},
                                {"label": "Correct only", "value": "correct"},
                                {"label": "Incorrect only", "value": "incorrect"},
                            ],
                            value="all",
                            inline=True,
                            className="mt-1",
                            inputStyle={"marginRight": "4px"},
                            labelStyle={"fontSize": "12px", "marginRight": "12px"},
                        ),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Div("ABS Status", className="filter-label"),
                        dbc.RadioItems(
                            id="filter-abs",
                            options=[
                                {"label": "All", "value": "all"},
                                {"label": "ABS Overturned", "value": "overturned"},
                            ],
                            value="all",
                            inline=True,
                            className="mt-1",
                            inputStyle={"marginRight": "4px"},
                            labelStyle={"fontSize": "12px", "marginRight": "12px"},
                        ),
                    ], xs=12, md=3),
                ], className="gy-2"),
            ], className="py-2"),
        ], className="filter-card mb-3"),

        # ── KPI cards ───────────────────────────────────────────────────────
        dbc.Row([
            _kpi_card("total",    "Called Pitches",    "white"),
            _kpi_card("pct",      "Correct Call %",    "green"),
            _kpi_card("phantom",  "Phantom Strikes",   "red"),
            _kpi_card("missed",   "Missed Strikes",    "orange"),
            _kpi_card("abs",      "ABS Overturned",    "purple"),
        ], className="mb-3 gy-2"),

        # ── Main content ────────────────────────────────────────────────────
        dbc.Row([
            # Zone plot (left)
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        dbc.Row([
                            dbc.Col(html.Span("Strike Zone", className="fw-semibold")),
                            dbc.Col(
                                dbc.ButtonGroup([
                                    dbc.Button("Scatter", id="btn-scatter", size="sm", n_clicks=0,
                                               color="primary", outline=False,
                                               style={"fontSize": "11px"}),
                                    dbc.Button("Density", id="btn-density", size="sm", n_clicks=0,
                                               color="secondary", outline=True,
                                               style={"fontSize": "11px"}),
                                ], size="sm"),
                                width="auto",
                            ),
                        ], align="center"),
                        className="py-2",
                    ),
                    dbc.CardBody([
                        dcc.Loading(
                            dcc.Graph(
                                id="zone-plot",
                                config={"displayModeBar": True, "modeBarButtonsToRemove": ["lasso2d"],
                                        "scrollZoom": True},
                                figure=_empty_fig("Apply filters to load pitches."),
                            ),
                            type="circle", color="#2ecc71",
                        ),
                        html.Div(id="plot-meta",
                                 style={"fontSize": "11px", "color": "#8b949e", "textAlign": "right",
                                        "marginTop": "4px"}),
                    ], className="p-2"),
                ], className="plot-card"),
            ], xs=12, lg=7),

            # Analysis panel (right)
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        dbc.Tabs(
                            id="analysis-tabs",
                            active_tab="tab-most-missed",
                            children=[
                                dbc.Tab(label="Most Missed",  tab_id="tab-most-missed"),
                                dbc.Tab(label="By Umpire",    tab_id="tab-umpire"),
                                dbc.Tab(label="By Pitcher",   tab_id="tab-pitcher"),
                                dbc.Tab(label="By Batter",    tab_id="tab-batter"),
                            ],
                            style={"fontSize": "12px"},
                        ),
                        className="p-0",
                    ),
                    dbc.CardBody(
                        dcc.Loading(
                            html.Div(id="analysis-content"),
                            type="circle", color="#3498db",
                        ),
                        className="p-2",
                    ),
                ], className="plot-card"),
            ], xs=12, lg=5),
        ], className="mb-3"),

        # Footer
        html.Hr(style={"borderColor": "#30363d"}),
        dbc.Row([
            dbc.Col(html.Small([
                "Data: MLB Statcast via ",
                html.A("Baseball Savant", href="https://baseballsavant.mlb.com",
                       target="_blank", style={"color": "#3498db"}),
                " | Incorrect call = pitch called ball/strike outside/inside the Statcast strike zone",
            ], style={"color": "#8b949e"})),
        ], className="mb-3"),

        # Hidden state store
        dcc.Store(id="filter-store", data={}),
        dcc.Store(id="view-store", data="scatter"),
    ], fluid=True, className="py-2")


app.layout = build_layout


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

def _collect_filters(start_date, end_date, umpires, teams, pitchers, batters,
                     pitch_types, p_throws, call_type, abs_filter):
    return {
        "start_date": start_date,
        "end_date": end_date,
        "umpires": umpires or [],
        "teams": teams or [],
        "pitchers": pitchers or [],
        "batters": batters or [],
        "pitch_types": pitch_types or [],
        "p_throws": p_throws or "B",
        "call_type": call_type or "all",
        "abs_filter": abs_filter or "all",
    }


# Update filter store on Apply click
@callback(
    Output("filter-store", "data"),
    Input("btn-apply", "n_clicks"),
    State("date-picker", "start_date"),
    State("date-picker", "end_date"),
    State("filter-umpire", "value"),
    State("filter-team", "value"),
    State("filter-pitcher", "value"),
    State("filter-batter", "value"),
    State("filter-pitch-type", "value"),
    State("filter-p-throws", "value"),
    State("filter-call-type", "value"),
    State("filter-abs", "value"),
    prevent_initial_call=False,
)
def store_filters(n_clicks, start_date, end_date, umpires, teams, pitchers,
                  batters, pitch_types, p_throws, call_type, abs_filter):
    return _collect_filters(start_date, end_date, umpires, teams, pitchers,
                            batters, pitch_types, p_throws, call_type, abs_filter)


# Reset all filters
@callback(
    Output("date-picker", "start_date"),
    Output("date-picker", "end_date"),
    Output("filter-umpire", "value"),
    Output("filter-team", "value"),
    Output("filter-pitcher", "value"),
    Output("filter-batter", "value"),
    Output("filter-pitch-type", "value"),
    Output("filter-p-throws", "value"),
    Output("filter-call-type", "value"),
    Output("filter-abs", "value"),
    Input("btn-reset", "n_clicks"),
    prevent_initial_call=True,
)
def reset_filters(_):
    first = get_first_game_date() or _season_start()
    last = get_last_game_date() or _today_str()
    return first, last, None, None, None, None, None, "B", "all", "all"


# Track scatter vs density view
@callback(
    Output("view-store", "data"),
    Output("btn-scatter", "color"),
    Output("btn-scatter", "outline"),
    Output("btn-density", "color"),
    Output("btn-density", "outline"),
    Input("btn-scatter", "n_clicks"),
    Input("btn-density", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_view(n_scatter, n_density):
    triggered = ctx.triggered_id
    if triggered == "btn-density":
        return "density", "secondary", True, "primary", False
    return "scatter", "primary", False, "secondary", True


# KPI cards
@callback(
    Output("kpi-total", "children"),
    Output("kpi-pct", "children"),
    Output("kpi-phantom", "children"),
    Output("kpi-missed", "children"),
    Output("kpi-abs", "children"),
    Input("filter-store", "data"),
)
def update_kpis(filters):
    if not filters:
        filters = {}
    s = query_summary_stats(filters)
    total = s.get("total", 0)
    correct = s.get("correct", 0)
    phantom = s.get("phantom_strikes", 0)
    missed = s.get("missed_strikes", 0)
    abs_ot = s.get("abs_overturned", 0)
    pct = f"{100 * correct / total:.1f}%" if total > 0 else "—"
    return (
        f"{total:,}",
        pct,
        f"{phantom:,}",
        f"{missed:,}",
        f"{abs_ot:,}",
    )


# Zone plot + meta
@callback(
    Output("zone-plot", "figure"),
    Output("plot-meta", "children"),
    Input("filter-store", "data"),
    Input("view-store", "data"),
)
def update_zone_plot(filters, view):
    filters = filters or {}
    total = query_summary_stats(filters).get("total", 0)
    pitches = query_pitches(filters, limit=15000)
    if not pitches:
        return _empty_fig("No pitches match the current filters."), ""
    meta = f"{len(pitches):,} pitches shown" + (f" of {total:,}" if total > len(pitches) else "")
    if view == "density":
        return build_density_figure(pitches, total), meta
    return build_scatter_figure(pitches, total), meta


# Analysis tabs content
@callback(
    Output("analysis-content", "children"),
    Input("filter-store", "data"),
    Input("analysis-tabs", "active_tab"),
)
def update_analysis(filters, active_tab):
    filters = filters or {}

    if active_tab == "tab-most-missed":
        rows = query_by_pitch_type_hand(filters)
        if not rows:
            return _no_data()
        df = pd.DataFrame(rows)
        df["pitch_name"] = df["pitch_type"].map(lambda x: PITCH_NAMES.get(x, x or "?"))
        df["hand"] = df["p_throws"].map({"R": "RHP", "L": "LHP"}).fillna("?")
        df["miss_rate"] = df["miss_rate"].map(lambda v: f"{v:.1f}%")
        cols_def = [
            {"name": "Pitch", "id": "pitch_name"},
            {"name": "Hand", "id": "hand"},
            {"name": "Called", "id": "called"},
            {"name": "Incorrect", "id": "incorrect"},
            {"name": "Miss %", "id": "miss_rate"},
        ]
        return html.Div([
            html.Div("Miss rate by pitch type × throwing hand (≥10 called pitches)",
                     className="section-header mt-1"),
            _make_table_static(
                cols_def,
                df[["pitch_name", "hand", "called", "incorrect", "miss_rate"]].to_dict("records"),
            ),
        ])

    if active_tab == "tab-umpire":
        rows = query_by_group("umpire", filters, limit=25)
        if not rows:
            return _no_data()
        cols_def = [
            {"name": "Umpire", "id": "group"},
            {"name": "Called", "id": "called"},
            {"name": "Incorrect", "id": "incorrect"},
            {"name": "Miss %", "id": "miss_rate"},
        ]
        data = [{"group": r["group"], "called": r["called"], "incorrect": r["incorrect"],
                 "miss_rate": f"{r['miss_rate']:.1f}%"} for r in rows]
        return html.Div([
            html.Div("Umpires ranked by miss rate (≥10 called pitches)",
                     className="section-header mt-1"),
            _make_table_static(cols_def, data),
        ])

    if active_tab == "tab-pitcher":
        rows = query_by_group("pitcher_name", filters, limit=25)
        if not rows:
            return _no_data()
        cols_def = [
            {"name": "Pitcher", "id": "group"},
            {"name": "Called", "id": "called"},
            {"name": "Incorrect", "id": "incorrect"},
            {"name": "Miss %", "id": "miss_rate"},
        ]
        data = [{"group": r["group"], "called": r["called"], "incorrect": r["incorrect"],
                 "miss_rate": f"{r['miss_rate']:.1f}%"} for r in rows]
        return html.Div([
            html.Div("Pitchers with highest miss-call rate against them (≥10 called)",
                     className="section-header mt-1"),
            _make_table_static(cols_def, data),
        ])

    if active_tab == "tab-batter":
        rows = query_by_group("batter_id", filters, limit=25)
        if not rows:
            return _no_data()
        cols_def = [
            {"name": "Batter", "id": "group"},
            {"name": "Called", "id": "called"},
            {"name": "Incorrect", "id": "incorrect"},
            {"name": "Miss %", "id": "miss_rate"},
        ]
        data = [{"group": r["group"], "called": r["called"], "incorrect": r["incorrect"],
                 "miss_rate": f"{r['miss_rate']:.1f}%"} for r in rows]
        return html.Div([
            html.Div("Batters with highest miss-call rate against them (≥10 called)",
                     className="section-header mt-1"),
            _make_table_static(cols_def, data),
        ])

    return _no_data()


def _make_table_static(cols, data):
    return dash_table.DataTable(
        columns=cols,
        data=data,
        page_size=14,
        style_table={"overflowX": "auto"},
        style_cell={"backgroundColor": "#1c2333", "color": "#e6edf3",
                    "border": "1px solid #30363d", "fontSize": "11.5px",
                    "textAlign": "left", "padding": "5px 9px",
                    "maxWidth": "180px", "overflow": "hidden",
                    "textOverflow": "ellipsis"},
        style_header={"backgroundColor": "#0d1117", "fontWeight": "bold",
                      "color": "#8b949e", "border": "1px solid #30363d",
                      "fontSize": "11px"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "rgba(255,255,255,0.02)"},
        ],
        sort_action="native",
        tooltip_data=[{c["id"]: {"value": str(row.get(c["id"], "")), "type": "markdown"}
                       for c in cols} for row in data],
        tooltip_duration=None,
    )


def _no_data():
    return html.Div("No data — apply filters and click Apply.", className="no-data-msg")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    app.run(debug=False, host="0.0.0.0", port=8050)
