#!/usr/bin/env python3
"""
dashboard.py — QuantBot Real-Time Web Dashboard
════════════════════════════════════════════════════════════════════
Opens at http://localhost:8050 — auto-refreshes every 15 seconds.

Install once:
  pip install dash plotly

Run alongside bot.py (separate terminal):
  python dashboard.py
  python dashboard.py --port 8080   # if 8050 is taken

Reads from DATA_DIR (shared Docker volume, or "." locally):
  bot_state.json     → live bot state
  corpus_state.json  → corpus + DCA
  trade_log.csv      → all trade history
════════════════════════════════════════════════════════════════════
"""

import json
import math
import traceback
import argparse
from datetime import datetime, timezone
from pathlib import Path
import os

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, dash_table
from dash.dependencies import Input, Output

# ── Config from .env ──────────────────────────────────────────────
DATA_DIR    = os.getenv("DATA_DIR", ".")
STATE_FILE  = os.path.join(DATA_DIR, "bot_state.json")
CORPUS_FILE = os.path.join(DATA_DIR, "corpus_state.json")
TRADE_LOG   = os.path.join(DATA_DIR, "trade_log.csv")
RSI_HISTORY = os.path.join(DATA_DIR, "rsi_history.json")
REFRESH_MS  = int(os.getenv("DASHBOARD_REFRESH_MS", "15000"))
DASH_PORT   = int(os.getenv("DASHBOARD_PORT", "8050"))
DASH_HOST   = os.getenv("DASHBOARD_HOST", "127.0.0.1")

# ── Benchmarks from backtest ──────────────────────────────────────
BENCH_WR = 0.124
BENCH_PF = 1.78

# ── Theme ─────────────────────────────────────────────────────────
BG    = "#0d1117"
SURF  = "#161b22"
BRD   = "#30363d"
TXT   = "#e6edf3"
MUTED = "#8b949e"
GRN   = "#3fb950"
RED   = "#f85149"
YLW   = "#d29922"
BLU   = "#58a6ff"
PRP   = "#bc8cff"

# !! IMPORTANT: PLBASE must NOT contain 'xaxis' or 'yaxis'.
# If PLBASE has yaxis={...} AND a chart function also passes yaxis={...}
# to update_layout(**PLBASE, yaxis=...) Python raises:
#   TypeError: got multiple values for keyword argument 'yaxis'
# Axis styling is applied exclusively via update_xaxes() / update_yaxes().
PLBASE = dict(
    paper_bgcolor=SURF,
    plot_bgcolor=BG,
    font=dict(color=TXT, size=12),
    margin=dict(l=50, r=20, t=40, b=40),
)
AXIS = dict(gridcolor=BRD, zerolinecolor=BRD, color=MUTED)


def ax(fig: go.Figure, y_suffix: str = "") -> go.Figure:
    """Apply consistent axis styling to any figure."""
    fig.update_xaxes(**AXIS)
    if y_suffix:
        fig.update_yaxes(**AXIS, ticksuffix=y_suffix)
    else:
        fig.update_yaxes(**AXIS)
    return fig


# ══════════════════════════════════════════════════════════════════
#  DATA LOADERS
# ══════════════════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_corpus() -> dict:
    try:
        with open(CORPUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load_trades() -> pd.DataFrame:
    try:
        if not Path(TRADE_LOG).exists():
            return pd.DataFrame()
        df = pd.read_csv(TRADE_LOG)
        if df.empty:
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df["date_str"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M")
        # normalise column names produced by bot.py
        if "pnl_usd" not in df.columns and "pnl" in df.columns:
            df["pnl_usd"] = df["pnl"]
        if "fees_usd" not in df.columns and "fees" in df.columns:
            df["fees_usd"] = df["fees"]
        return df.sort_values("datetime", ascending=False)
    except Exception:
        return pd.DataFrame()


def load_rsi_history() -> pd.DataFrame:
    try:
        if not Path(RSI_HISTORY).exists():
            return pd.DataFrame()
        with open(RSI_HISTORY) as f:
            data = json.load(f)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        return df
    except Exception:
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
#  METRICS ENGINE
# ══════════════════════════════════════════════════════════════════

def calc_metrics(trades: pd.DataFrame, state: dict, corpus: dict) -> dict:
    m = {}
    m["balance"]   = state.get("balance", 100.0)
    m["corpus"]    = corpus.get("corpus", m["balance"])
    m["start_bal"] = state.get("start_balance", 100.0)
    m["total_inv"] = m["start_bal"] + corpus.get("total_dca_added", 0.0)
    m["net_pnl"]   = m["balance"] - m["total_inv"]
    m["ret_pct"]   = m["net_pnl"] / m["total_inv"] * 100 if m["total_inv"] > 0 else 0

    m["n_trades"]    = state.get("total_trades", 0)
    m["n_wins"]      = state.get("total_wins", 0)
    m["n_losses"]    = m["n_trades"] - m["n_wins"]
    m["win_rate"]    = m["n_wins"] / m["n_trades"] * 100 if m["n_trades"] > 0 else 0
    m["consec_loss"] = state.get("consecutive_losses", 0)
    m["total_fees"]  = state.get("total_fees", 0.0)

    # Uptime
    start_str = state.get("start_date")
    if start_str:
        try:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(start_str)
            d, rem = divmod(int(delta.total_seconds()), 86400)
            h, rem = divmod(rem, 3600)
            mn     = rem // 60
            m["uptime"] = f"{d}d {h}h {mn}m"
            m["years"]  = delta.total_seconds() / (365.25 * 86400)
        except Exception:
            m["uptime"], m["years"] = "—", 1.0
    else:
        m["uptime"], m["years"] = "—", 1.0

    m["annual"] = m["ret_pct"] / m["years"] if m["years"] > 0 else 0

    # defaults
    for k in ["pf","sharpe","sortino","max_dd","calmar",
              "avg_win","avg_loss","rr","best","worst",
              "avg_hold_h","avg_hold_c","max_ws","max_ls","expect"]:
        m.setdefault(k, 0.0)

    if trades.empty:
        return m

    pnls    = trades["pnl_usd"].values
    wins_v  = pnls[pnls > 0]
    loss_v  = pnls[pnls <= 0]
    gw = wins_v.sum() if len(wins_v) else 0
    gl = abs(loss_v.sum()) if len(loss_v) else 0
    m["pf"]       = gw / gl if gl > 0 else 999.0
    m["avg_win"]  = float(wins_v.mean())      if len(wins_v) else 0
    m["avg_loss"] = float(abs(loss_v.mean())) if len(loss_v) else 0
    m["rr"]       = m["avg_win"] / m["avg_loss"] if m["avg_loss"] > 0 else 0
    m["best"]     = float(pnls.max())
    m["worst"]    = float(pnls.min())

    if "hold_candles" in trades.columns:
        m["avg_hold_c"] = float(trades["hold_candles"].mean())
        m["avg_hold_h"] = m["avg_hold_c"] * 0.25
    else:
        m["avg_hold_c"] = m["avg_hold_h"] = 0.0

    if "balance" in trades.columns:
        chron   = trades.sort_values("datetime")
        eq      = np.concatenate([[m["start_bal"]], chron["balance"].values])
        rets    = np.diff(eq) / np.where(eq[:-1] == 0, 1, eq[:-1])
        cpy     = 365 * 24 * 4
        if rets.std() > 0:
            m["sharpe"] = float((rets.mean() / rets.std()) * math.sqrt(cpy))
        neg = rets[rets < 0]
        if len(neg) > 0 and neg.std() > 0:
            m["sortino"] = float((rets.mean() / neg.std()) * math.sqrt(cpy))
        peaks       = np.maximum.accumulate(eq)
        dd          = (peaks - eq) / np.where(peaks == 0, 1, peaks) * 100
        m["max_dd"] = float(dd.max())
        m["calmar"] = m["annual"] / m["max_dd"] if m["max_dd"] > 0 else 0

    # Streaks
    mw = ml = cur = 0
    for p in trades.sort_values("datetime")["pnl_usd"]:
        if p > 0:
            cur = max(0, cur) + 1; mw = max(mw, cur)
        else:
            cur = min(0, cur) - 1; ml = max(ml, abs(cur))
    m["max_ws"] = mw
    m["max_ls"] = ml
    m["expect"] = (m["win_rate"]/100)*m["avg_win"] - (1 - m["win_rate"]/100)*m["avg_loss"]

    return m


# ══════════════════════════════════════════════════════════════════
#  CHART BUILDERS
# ══════════════════════════════════════════════════════════════════

def chart_equity(trades: pd.DataFrame, state: dict) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "balance" in trades.columns:
        df = trades.sort_values("datetime")
        f.add_trace(go.Scatter(
            x=df["datetime"], y=df["balance"], mode="lines",
            line=dict(color=BLU, width=2), fill="tozeroy",
            fillcolor="rgba(88,166,255,0.08)", name="Balance"))
        if "pnl_usd" in df.columns:
            w = df[df["pnl_usd"] > 0]
            l = df[df["pnl_usd"] <= 0]
            if not w.empty:
                f.add_trace(go.Scatter(x=w["datetime"], y=w["balance"],
                    mode="markers", marker=dict(color=GRN, size=7), name="Win"))
            if not l.empty:
                f.add_trace(go.Scatter(x=l["datetime"], y=l["balance"],
                    mode="markers", marker=dict(color=RED, size=7, symbol="x"), name="Loss"))
    bal = state.get("balance", 0)
    f.update_layout(**PLBASE, height=310,
        title=dict(text=f"Equity Curve  (${bal:,.2f})", font=dict(size=13)),
        legend=dict(bgcolor="rgba(0,0,0,0)", x=0, y=1))
    return ax(f)


def chart_drawdown(trades: pd.DataFrame, start_bal: float) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "balance" in trades.columns:
        df   = trades.sort_values("datetime")
        eq   = np.concatenate([[start_bal], df["balance"].values])
        pk   = np.maximum.accumulate(eq)
        dd   = (pk - eq) / np.where(pk == 0, 1, pk) * 100
        dts  = [None] + df["datetime"].tolist()
        f.add_trace(go.Scatter(x=dts, y=-dd, mode="lines", fill="tozeroy",
            fillcolor="rgba(248,81,73,0.12)", line=dict(color=RED, width=1.5),
            showlegend=False))
    f.update_layout(**PLBASE, height=200, showlegend=False,
        title=dict(text="Drawdown %", font=dict(size=13)))
    return ax(f, "%")


def chart_pnl_hist(trades: pd.DataFrame) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "pnl_usd" in trades.columns:
        p = trades["pnl_usd"]
        f.add_trace(go.Histogram(x=p[p > 0],  name="Wins",
            marker_color=GRN, opacity=0.8, nbinsx=20))
        f.add_trace(go.Histogram(x=p[p <= 0], name="Losses",
            marker_color=RED, opacity=0.8, nbinsx=20))
    f.update_layout(**PLBASE, height=270, barmode="overlay",
        title=dict(text="P&L Distribution", font=dict(size=13)),
        legend=dict(bgcolor="rgba(0,0,0,0)"))
    return ax(f)


def chart_monthly(trades: pd.DataFrame) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "pnl_usd" in trades.columns:
        df  = trades.sort_values("datetime").copy()
        df["month"] = df["datetime"].dt.to_period("M").astype(str)
        mon = df.groupby("month")["pnl_usd"].sum().reset_index()
        cols = [GRN if v >= 0 else RED for v in mon["pnl_usd"]]
        f.add_trace(go.Bar(x=mon["month"], y=mon["pnl_usd"],
            marker_color=cols, showlegend=False))
    f.update_layout(**PLBASE, height=270, showlegend=False,
        title=dict(text="Monthly P&L", font=dict(size=13)))
    return ax(f)


def chart_side(trades: pd.DataFrame) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "side" in trades.columns and "pnl_usd" in trades.columns:
        g = trades.groupby("side").agg(
            total=("pnl_usd", "sum"),
            n=("pnl_usd", "count"),
            wins=("pnl_usd", lambda x: (x > 0).sum()),
        ).reset_index()
        cols = [GRN if v >= 0 else RED for v in g["total"]]
        txt  = [f"n={r['n']}  WR={r['wins']/r['n']*100:.0f}%" for _, r in g.iterrows()]
        f.add_trace(go.Bar(x=g["side"], y=g["total"],
            marker_color=cols, text=txt, textposition="outside", showlegend=False))
    f.update_layout(**PLBASE, height=240, showlegend=False,
        title=dict(text="Long vs Short P&L", font=dict(size=13)))
    return ax(f)


def chart_rolling_wr(trades: pd.DataFrame, window: int = 10) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "pnl_usd" in trades.columns and len(trades) >= window:
        df = trades.sort_values("datetime").copy()
        df["rwr"] = (df["pnl_usd"] > 0).rolling(window).mean() * 100
        df = df.dropna(subset=["rwr"])
        f.add_trace(go.Scatter(x=df["datetime"], y=df["rwr"], mode="lines",
            line=dict(color=PRP, width=2), showlegend=False))
        f.add_hline(y=BENCH_WR * 100, line=dict(color=YLW, dash="dash", width=1),
                    annotation_text=f"Bench {BENCH_WR*100:.1f}%",
                    annotation_font_color=YLW)
    f.update_layout(**PLBASE, height=220, showlegend=False,
        title=dict(text=f"Rolling {window}-Trade Win Rate", font=dict(size=13)))
    return ax(f, "%")


def chart_cumulative(trades: pd.DataFrame) -> go.Figure:
    f = go.Figure()
    if not trades.empty and "pnl_usd" in trades.columns:
        df = trades.sort_values("datetime").copy()
        df["cum"] = df["pnl_usd"].cumsum()
        f.add_trace(go.Scatter(x=df["datetime"], y=df["cum"], mode="lines",
            line=dict(color=GRN, width=2), fill="tozeroy",
            fillcolor="rgba(63,185,80,0.08)", showlegend=False))
        f.add_hline(y=0, line=dict(color=BRD, width=1))
    f.update_layout(**PLBASE, height=220, showlegend=False,
        title=dict(text="Cumulative P&L", font=dict(size=13)))
    return ax(f)


# ══════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════

def kpi(label, value, sub="", val_color=TXT):
    return html.Div([
        html.Div(label, style={"color":MUTED, "fontSize":"10px",
                               "textTransform":"uppercase", "letterSpacing":"1px",
                               "marginBottom":"4px"}),
        html.Div(str(value), style={"color":val_color, "fontSize":"19px",
                                    "fontWeight":"700", "lineHeight":"1.2"}),
        html.Div(sub, style={"color":MUTED, "fontSize":"10px", "marginTop":"3px"}),
    ], style={"background":SURF, "border":f"1px solid {BRD}", "borderRadius":"8px",
              "padding":"12px 14px", "flex":"1", "minWidth":"120px"})


def dcol(v):
    return GRN if v >= 0 else RED


# ══════════════════════════════════════════════════════════════════
#  DASH APP
# ══════════════════════════════════════════════════════════════════

app = Dash(__name__, title="QuantBot", update_title=None)
TAB_STYLE        = {"padding": "8px 20px", "fontWeight": "500",
                    "color": MUTED, "borderBottom": f"2px solid {BRD}",
                    "background": BG, "border": "none", "cursor": "pointer"}
TAB_SELECTED     = {**TAB_STYLE, "color": BLU, "borderBottom": f"2px solid {BLU}"}

app.layout = html.Div([
    dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),

    # Header
    html.Div([
        html.Div([
            html.Span("⚡", style={"marginRight": "6px"}),
            html.Span("QuantBot", style={"fontWeight": "700", "color": BLU}),
            html.Span(" Dashboard", style={"color": MUTED}),
        ], style={"fontSize": "18px"}),
        html.Div(id="hdr-mid", style={"color": GRN, "fontSize": "13px"}),
        html.Div(id="hdr-time", style={"color": MUTED, "fontSize": "11px"}),
    ], style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
              "background": SURF, "borderBottom": f"1px solid {BRD}", "padding": "13px 24px"}),

    # Tabs
    dcc.Tabs(id="tabs", value="tab-overview", children=[
        dcc.Tab(label="📈  Overview", value="tab-overview",
                style=TAB_STYLE, selected_style=TAB_SELECTED),
        dcc.Tab(label="🔭  RSI Radar", value="tab-rsi",
                style=TAB_STYLE, selected_style=TAB_SELECTED),
    ], style={"background": BG, "borderBottom": f"1px solid {BRD}",
              "paddingLeft": "24px"}),

    html.Div(id="tab-content"),

    # Footer watermark
    html.Div([
        html.Span("⚡ Built by ", style={"color": MUTED}),
        html.Span("Asit Minz", style={
            "color": BLU, "fontWeight": "700", "letterSpacing": "0.5px"
        }),
        html.Span("  ·  ", style={"color": BRD}),
        html.Span("Trained on caffeine. Powered by backtest.", style={
            "color": MUTED, "fontSize": "10px"
        }),
        html.Span("  ·  ", style={"color": BRD}),
        html.Span("Not financial advice. Just vibes and RSI divergence.", style={
            "color": MUTED, "fontSize": "10px"
        }),
    ], style={
        "textAlign": "center",
        "padding": "18px 24px",
        "fontSize": "11px",
        "borderTop": f"1px solid {BRD}",
        "marginTop": "32px",
        "letterSpacing": "0.3px",
    }),

], style={"backgroundColor": BG, "color": TXT,
          "fontFamily": "'Inter','Segoe UI',system-ui,sans-serif", "minHeight": "100vh"})


# ── Tab router ────────────────────────────────────────────────────
@app.callback(Output("tab-content", "children"), Input("tabs", "value"))
def render_tab(tab):
    if tab == "tab-rsi":
        return html.Div([
            # Current readings gauges
            html.Div([
                html.Div("Current RSI Readings", style={
                    "color": MUTED, "fontSize": "10px", "textTransform": "uppercase",
                    "letterSpacing": "1px", "marginBottom": "12px"}),
                html.Div(id="rsi-gauges",
                         style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
            ], style={"marginBottom": "24px"}),

            # Extreme events table
            html.Div([
                html.Div("RSI Extreme Events  (< 20 oversold  |  > 80 overbought)",
                         style={"color": MUTED, "fontSize": "10px", "textTransform": "uppercase",
                                "letterSpacing": "1px", "marginBottom": "12px"}),
                html.Div(id="rsi-extremes-tbl"),
            ], style={"marginBottom": "24px"}),

            # RSI over time line chart
            html.Div([
                html.Div("RSI History by Coin", style={
                    "color": MUTED, "fontSize": "10px", "textTransform": "uppercase",
                    "letterSpacing": "1px", "marginBottom": "12px"}),
                dcc.Graph(id="rsi-chart", config={"displayModeBar": False}),
            ]),
        ], style={"padding": "18px 24px", "maxWidth": "1900px", "margin": "0 auto"})

    # Default: Overview tab
    return html.Div([
        html.Div(id="row-kpi",
                 style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "marginBottom": "14px"}),
        html.Div(id="row-quality",
                 style={"display": "flex", "gap": "8px", "flexWrap": "wrap", "marginBottom": "16px"}),
        html.Div([
            html.Div([
                dcc.Graph(id="ch-eq",  config={"displayModeBar": False}),
                dcc.Graph(id="ch-dd",  config={"displayModeBar": False}),
            ], style={"flex": "3"}),
            html.Div([
                dcc.Graph(id="ch-side", config={"displayModeBar": False}),
                dcc.Graph(id="ch-rwr",  config={"displayModeBar": False}),
            ], style={"flex": "2"}),
        ], style={"display": "flex", "gap": "16px", "marginBottom": "16px"}),
        html.Div([
            dcc.Graph(id="ch-hist",    config={"displayModeBar": False}, style={"flex": "1"}),
            dcc.Graph(id="ch-monthly", config={"displayModeBar": False}, style={"flex": "1"}),
            dcc.Graph(id="ch-cum",     config={"displayModeBar": False}, style={"flex": "1"}),
        ], style={"display": "flex", "gap": "16px", "marginBottom": "16px"}),
        html.Div(id="pos-card", style={"marginBottom": "16px"}),
        html.Div([
            html.Div("All Trades", style={"color": MUTED, "fontSize": "10px",
                "textTransform": "uppercase", "letterSpacing": "1px", "marginBottom": "10px"}),
            html.Div(id="trade-tbl"),
        ]),
    ], style={"padding": "18px 24px", "maxWidth": "1900px", "margin": "0 auto"})


@app.callback(
    [Output("hdr-mid", "children"),   Output("hdr-time", "children"),
     Output("row-kpi", "children"),   Output("row-quality", "children"),
     Output("ch-eq", "figure"),       Output("ch-dd", "figure"),
     Output("ch-hist", "figure"),     Output("ch-monthly", "figure"),
     Output("ch-side", "figure"),     Output("ch-rwr", "figure"),
     Output("ch-cum", "figure"),      Output("pos-card", "children"),
     Output("trade-tbl", "children")],
    Input("tick", "n_intervals"),
)
def refresh(_):
    try:
        state  = load_state()
        corpus = load_corpus()
        trades = load_trades()
        m      = calc_metrics(trades, state, corpus)
        now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode   = state.get("mode","?").upper()

        # Header middle
        pos = state.get("position")
        cb  = state.get("cb_pause_until")
        if cb:
            try:
                resume = datetime.fromisoformat(cb)
                hrs    = (resume - datetime.now(timezone.utc)).total_seconds() / 3600
                hdr_mid = f"🛑 CB PAUSE — {max(0,hrs):.1f}h remaining" if hrs > 0 else "✅ Watching"
            except Exception:
                hdr_mid = "⚠️ CB status unknown"
        elif pos:
            side = pos.get("side","?").upper()
            ep   = pos.get("entry_price",0)
            hdr_mid = f"{'📈' if side=='LONG' else '📉'} {side} @ ${ep:,.0f}"
        else:
            hdr_mid = f"✅ Watching — {mode}"

        hdr_time = f"Refreshed {now}  |  Uptime {m['uptime']}  |  Every {REFRESH_MS//1000}s"

        # KPI row
        row_kpi = [
            kpi("Balance",      f"${m['balance']:,.2f}",
                f"started ${m['start_bal']:.2f}"),
            kpi("Net Profit",   f"${m['net_pnl']:+,.2f}",
                f"invested ${m['total_inv']:.2f}", dcol(m["net_pnl"])),
            kpi("Total Return", f"{m['ret_pct']:+.1f}%",
                f"{m['annual']:+.1f}% / yr", dcol(m["ret_pct"])),
            kpi("Corpus",       f"${m['corpus']:,.2f}",
                f"DCA ${corpus.get('total_dca_added',0):.2f} added"),
            kpi("Trades",       str(m["n_trades"]),
                f"{m['n_wins']}W  {m['n_losses']}L"),
            kpi("Win Rate",     f"{m['win_rate']:.1f}%",
                f"bench {BENCH_WR*100:.1f}%",
                GRN if m["win_rate"] >= BENCH_WR*80 else RED),
            kpi("Fees Paid",    f"${m['total_fees']:.2f}",
                "0.05% each side"),
            kpi("Consec Loss",  str(m["consec_loss"]),
                f"CB at 5  |  {mode}",
                RED if m["consec_loss"] >= 4 else TXT),
        ]

        # Quality row
        row_q = [
            kpi("Profit Factor", f"{m['pf']:.2f}",
                f"bench {BENCH_PF}",
                GRN if m["pf"] >= BENCH_PF else (YLW if m["pf"] >= 1.0 else RED)),
            kpi("Sharpe",  f"{m['sharpe']:.2f}", "annualised (15m)",
                GRN if m["sharpe"] >= 1 else YLW),
            kpi("Sortino", f"{m['sortino']:.2f}", "downside-adj.",
                GRN if m["sortino"] >= 1 else YLW),
            kpi("Max DD",  f"{m['max_dd']:.1f}%", "",
                GRN if m["max_dd"] < 15 else (YLW if m["max_dd"] < 25 else RED)),
            kpi("Calmar",  f"{m['calmar']:.2f}",  "annual / max DD"),
            kpi("Avg Win", f"${m['avg_win']:,.2f}", f"best ${m['best']:,.2f}", GRN),
            kpi("Avg Loss",f"${m['avg_loss']:,.2f}",f"worst ${m['worst']:,.2f}", RED),
            kpi("R:R",     f"{m['rr']:.2f}×",   "avg_win / avg_loss"),
            kpi("Expectancy", f"${m['expect']:+.2f}", "per trade", dcol(m["expect"])),
            kpi("Avg Hold",   f"{m['avg_hold_h']:.1f}h", f"{m['avg_hold_c']:.0f} candles"),
            kpi("Win Streak", f"▲{m['max_ws']}", "best run", GRN),
            kpi("Loss Streak",f"▼{m['max_ls']}", "worst run", RED),
        ]

        # Charts
        fe   = chart_equity(trades, state)
        fdd  = chart_drawdown(trades, m["start_bal"])
        fh   = chart_pnl_hist(trades)
        fmon = chart_monthly(trades)
        fsd  = chart_side(trades)
        frwr = chart_rolling_wr(trades)
        fcum = chart_cumulative(trades)

        # Position card
        if pos:
            side  = pos.get("side","?").upper()
            entry = pos.get("entry_price", 0)
            stop  = pos.get("stop_price", 0)
            qty   = pos.get("quantity", 0)
            mgn   = pos.get("margin", 0)
            et    = str(pos.get("entry_time","?"))[:19]
            dist  = abs(entry - stop) / entry * 100 if entry else 0
            brd_c = "#238636" if side == "LONG" else "#b62324"
            clr_s = GRN if side == "LONG" else RED
            pc = html.Div([
                html.Div([
                    html.Span(f"{'🟢' if side=='LONG' else '🔴'} {side}",
                              style={"fontWeight":"700","fontSize":"15px","color":clr_s}),
                    html.Span(f"  entry ${entry:,.2f}",    style={"color":TXT}),
                    html.Span(f"  |  stop ${stop:,.2f} ({dist:.2f}% away)",
                              style={"color":YLW}),
                    html.Span(f"  |  qty {qty:.6f} BTC",   style={"color":MUTED}),
                    html.Span(f"  |  margin ${mgn:.2f}",   style={"color":MUTED}),
                    html.Span(f"  |  since {et}",          style={"color":MUTED}),
                ]),
            ], style={"background":SURF,"border":f"1px solid {brd_c}",
                      "borderRadius":"8px","padding":"14px 18px"})
        else:
            if cb:
                try:
                    resume = datetime.fromisoformat(cb)
                    hrs    = (resume - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hrs > 0:
                        msg = (f"🛑 Circuit Breaker active — {hrs:.1f}h left  "
                               f"(resumes {resume.strftime('%Y-%m-%d %H:%M UTC')})")
                        c, b = YLW, f"1px solid {YLW}"
                    else:
                        msg, c, b = "✅ Watching — no open position", GRN, f"1px solid {BRD}"
                except Exception:
                    msg, c, b = "CB status unknown", MUTED, f"1px solid {BRD}"
            else:
                msg, c, b = "✅ Watching for signals — no open position", GRN, f"1px solid {BRD}"
            pc = html.Div(msg, style={"background":SURF,"border":b,
                                      "borderRadius":"8px","padding":"14px 18px","color":c})

        # Trade table
        if not trades.empty:
            want   = ["date_str","side","entry_price","exit_price","stop_price",
                      "pnl_usd","fees_usd","balance","reason","hold_candles","mode"]
            labels = {"date_str":"Date","side":"Side","entry_price":"Entry $",
                      "exit_price":"Exit $","stop_price":"Stop $","pnl_usd":"P&L $",
                      "fees_usd":"Fees $","balance":"Balance $","reason":"Reason",
                      "hold_candles":"Hold (c)","mode":"Mode"}
            cols   = [c for c in want if c in trades.columns]
            df_d   = trades[cols].copy()
            for c in ["entry_price","exit_price","stop_price","pnl_usd","fees_usd","balance"]:
                if c in df_d.columns:
                    df_d[c] = df_d[c].round(2)
            df_d.columns = [labels.get(c, c) for c in cols]
            tbl = dash_table.DataTable(
                data=df_d.to_dict("records"),
                columns=[{"name":n,"id":n} for n in df_d.columns],
                page_size=25, sort_action="native", filter_action="native",
                style_table={"overflowX":"auto"},
                style_header={"backgroundColor":SURF,"color":MUTED,"fontWeight":"600",
                    "fontSize":"10px","textTransform":"uppercase","letterSpacing":"1px",
                    "border":f"1px solid {BRD}"},
                style_cell={"backgroundColor":BG,"color":TXT,"border":f"1px solid {BRD}",
                    "fontSize":"12px","padding":"8px 12px",
                    "fontFamily":"'JetBrains Mono','Fira Code','Courier New',monospace"},
                style_data_conditional=[
                    {"if":{"filter_query":"{P&L $} > 0"}, "color":GRN},
                    {"if":{"filter_query":"{P&L $} < 0"}, "color":RED},
                    {"if":{"column_id":"Side","filter_query":"{Side} eq 'long'"},"color":GRN},
                    {"if":{"column_id":"Side","filter_query":"{Side} eq 'short'"},"color":RED},
                ],
            )
        else:
            tbl = html.Div("No trades yet — bot is watching for signals.",
                style={"color":MUTED,"padding":"24px","background":SURF,
                       "borderRadius":"8px","border":f"1px solid {BRD}",
                       "textAlign":"center"})

        return (hdr_mid, hdr_time, row_kpi, row_q,
                fe, fdd, fh, fmon, fsd, frwr, fcum, pc, tbl)

    except Exception as e:
        tb  = traceback.format_exc()
        ef  = go.Figure().update_layout(**PLBASE)
        err = html.Div([
            html.Div(f"Dashboard error: {e}", style={"color":RED}),
            html.Pre(tb, style={"color":MUTED,"fontSize":"10px"}),
        ])
        return (f"Error: {e}", "", [], [],
                ef, ef, ef, ef, ef, ef, ef, err, err)


# ── RSI Radar callback ────────────────────────────────────────────
@app.callback(
    [Output("rsi-gauges",       "children"),
     Output("rsi-extremes-tbl", "children"),
     Output("rsi-chart",        "figure")],
    Input("tick", "n_intervals"),
)
def refresh_rsi(_):
    empty_fig = go.Figure().update_layout(**PLBASE)
    no_data   = html.Div("No RSI history yet — notifier runs its first scan a few minutes after startup.",
                         style={"color": MUTED, "padding": "24px", "background": SURF,
                                "borderRadius": "8px", "border": f"1px solid {BRD}",
                                "textAlign": "center"})
    try:
        df = load_rsi_history()
        if df.empty:
            return [], no_data, empty_fig

        # ── 1. GAUGES — latest reading per coin ───────────────────
        latest = df.groupby("coin").tail(1).reset_index(drop=True)
        gauges  = []
        for _, row in latest.iterrows():
            rsi   = row["rsi"]
            coin  = row["coin"]
            tf    = row["tf"]
            zone  = row["zone"]
            ts    = row["ts"]

            if zone == "oversold":
                border_color = GRN
                badge_bg     = "#0d2818"
                badge_txt    = "OVERSOLD 🟢"
            elif zone == "overbought":
                border_color = RED
                badge_bg     = "#2d0f0f"
                badge_txt    = "OVERBOUGHT 🔴"
            else:
                border_color = BRD
                badge_bg     = SURF
                badge_txt    = "Neutral"

            # colour the RSI number itself
            if rsi <= 30:
                rsi_color = GRN
            elif rsi >= 70:
                rsi_color = RED
            elif rsi <= 40 or rsi >= 60:
                rsi_color = YLW
            else:
                rsi_color = TXT

            gauges.append(html.Div([
                html.Div(coin, style={"fontWeight": "700", "fontSize": "15px",
                                      "marginBottom": "4px"}),
                html.Div(f"{tf}", style={"color": MUTED, "fontSize": "10px",
                                          "marginBottom": "8px"}),
                html.Div(f"{rsi:.1f}", style={"fontSize": "32px", "fontWeight": "800",
                                               "color": rsi_color, "lineHeight": "1"}),
                html.Div("RSI", style={"color": MUTED, "fontSize": "10px",
                                       "marginBottom": "8px"}),
                html.Div(badge_txt, style={"fontSize": "10px", "padding": "3px 8px",
                                           "background": badge_bg, "borderRadius": "4px",
                                           "color": border_color, "fontWeight": "600",
                                           "marginBottom": "8px"}),
                html.Div(ts, style={"color": MUTED, "fontSize": "9px"}),
            ], style={
                "background": SURF, "border": f"1px solid {border_color}",
                "borderRadius": "10px", "padding": "16px 20px",
                "minWidth": "130px", "textAlign": "center",
            }))

        # ── 2. EXTREMES TABLE — all readings < 20 or > 80 ────────
        extremes = df[df["zone"].isin(["oversold", "overbought"])].copy()
        extremes = extremes.sort_values("ts", ascending=False).reset_index(drop=True)

        if extremes.empty:
            extremes_section = html.Div(
                "No RSI extremes recorded yet — thresholds are < 20 (oversold) and > 80 (overbought).",
                style={"color": MUTED, "padding": "20px", "background": SURF,
                       "borderRadius": "8px", "border": f"1px solid {BRD}",
                       "textAlign": "center", "fontSize": "13px"})
        else:
            rows = []
            for _, r in extremes.iterrows():
                is_os   = r["zone"] == "oversold"
                z_color = GRN if is_os else RED
                rows.append(html.Tr([
                    html.Td(r["ts"],
                            style={"color": MUTED,   "padding": "8px 14px", "fontSize": "12px"}),
                    html.Td(r["coin"],
                            style={"fontWeight": "700", "padding": "8px 14px"}),
                    html.Td(r["tf"],
                            style={"color": MUTED,   "padding": "8px 14px"}),
                    html.Td(f"{r['rsi']:.1f}",
                            style={"color": z_color, "padding": "8px 14px",
                                   "fontWeight": "700", "textAlign": "right"}),
                    html.Td("OVERSOLD 📉" if is_os else "OVERBOUGHT 📈",
                            style={"color": z_color, "padding": "8px 14px", "fontSize": "11px"}),
                    html.Td(f"${r['price']:,.4f}",
                            style={"color": TXT,     "padding": "8px 14px",
                                   "textAlign": "right", "fontFamily": "monospace"}),
                ]))

            extremes_section = html.Table(
                [html.Thead(html.Tr([
                    html.Th(h, style={"color": MUTED, "padding": "8px 14px", "fontSize": "10px",
                                      "textTransform": "uppercase", "letterSpacing": "1px",
                                      "textAlign": "left", "fontWeight": "500",
                                      "borderBottom": f"1px solid {BRD}"})
                    for h in ["Timestamp", "Coin", "Timeframe", "RSI", "Zone", "Price"]
                ])),
                 html.Tbody(rows)],
                style={"width": "100%", "borderCollapse": "collapse",
                       "background": SURF, "borderRadius": "8px",
                       "border": f"1px solid {BRD}"}
            )

        # ── 3. LINE CHART — RSI over time per coin ────────────────
        fig = go.Figure()

        # Oversold / overbought reference bands
        fig.add_hrect(y0=0,  y1=20, fillcolor=GRN, opacity=0.06, line_width=0,
                      annotation_text="Oversold < 20", annotation_position="left",
                      annotation_font_color=GRN, annotation_font_size=10)
        fig.add_hrect(y0=80, y1=100, fillcolor=RED, opacity=0.06, line_width=0,
                      annotation_text="Overbought > 80", annotation_position="left",
                      annotation_font_color=RED, annotation_font_size=10)
        fig.add_hline(y=20, line_dash="dot", line_color=GRN, line_width=1)
        fig.add_hline(y=80, line_dash="dot", line_color=RED, line_width=1)
        fig.add_hline(y=50, line_dash="dot", line_color=BRD, line_width=1)

        COIN_COLORS = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#bc8cff", "#ff7b72"]
        for i, (coin, grp) in enumerate(df.groupby("coin")):
            grp = grp.sort_values("ts")
            fig.add_trace(go.Scatter(
                x=grp["ts"], y=grp["rsi"],
                name=coin,
                mode="lines+markers",
                line=dict(color=COIN_COLORS[i % len(COIN_COLORS)], width=2),
                marker=dict(size=5),
                hovertemplate=(
                    f"<b>{coin}</b><br>"
                    "RSI: %{y:.1f}<br>"
                    "Time: %{x}<br>"
                    "<extra></extra>"
                )
            ))

        fig.update_layout(
            **PLBASE,
            height=340,
            yaxis=dict(range=[0, 100], gridcolor=BRD, ticksuffix="",
                       title=dict(text="RSI", font=dict(color=MUTED, size=11))),
            xaxis=dict(gridcolor=BRD),
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0, font=dict(color=MUTED, size=11)),
            hovermode="x unified",
        )

        return gauges, extremes_section, fig

    except Exception as e:
        tb  = traceback.format_exc()
        err = html.Div([
            html.Div(f"RSI Radar error: {e}", style={"color": RED}),
            html.Pre(tb, style={"color": MUTED, "fontSize": "10px"}),
        ])
        return [], err, empty_fig


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="QuantBot Real-Time Dashboard")
    ap.add_argument("--port", type=int, default=DASH_PORT, help="Port (default 8050)")
    ap.add_argument("--host", default=DASH_HOST,           help="Host (default 127.0.0.1)")
    args = ap.parse_args()
    print(f"\n  ✅  Dashboard → http://{args.host}:{args.port}")
    print(f"  Auto-refreshes every {REFRESH_MS//1000}s\n")
    app.run(debug=False, host=args.host, port=args.port)