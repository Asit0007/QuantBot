"""
research/sar_bb_grid.py — SAR + Bollinger "scale out in a mature trend",
swept as a full permutation grid, with the search cost measured.

THE STRATEGY (Asit's specification)
  Regime   price above its 200-day MA for N consecutive days = "prolonged bull"
  Entry    SAR flips bullish (sar < close) while in that regime
  Scale    on each touch of the upper Bollinger band, sell FRAC of the
           position and keep the rest running
  Exit     SAR flips bearish -> close the remainder
  Short    optional: on an upper-band touch in a prolonged bull, open a short
           (fading INTO strength — the part nothing here has ever tested)

WHY THIS IS NOT A REPEAT OF backtest_bbsar.py
  That script tested BREAKOUT (long above the upper band) and REVERSION (long
  off the lower band, shorts requiring SAR already BEARISH). Neither fades a
  still-intact uptrend, and NOTHING in this repo has ever used trend AGE as an
  input — only trend direction. It also swept 5m-4h and stopped there, with
  the log recording lower timeframes as "strictly worse, monotonically". Daily
  was never reached.

ON RUNNING A BIG GRID AT ALL
  A sweep is selection, and selection is what the deflated Sharpe exists to
  punish. Running one is defensible only if the cost is PAID rather than
  hidden, so this script:
    * reports the FULL distribution, not the winner
    * feeds the true grid size into gate 6 (deflated Sharpe)
    * computes gate 7 (PBO) across the grid — finally possible, since PBO
      needs >= 2 configs and every prior test had exactly one
  Expect the best config to look good and the DSR to say it is noise. That
  disagreement IS the deliverable.
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from overfitting import deflated_sharpe, pbo                      # noqa: E402
from robustness import profit_factor, run_gates                   # noqa: E402

FEE = {"BTC": 0.0005, "NSE": 0.0010}      # per side


# ── grid ─────────────────────────────────────────────────────────────
GRID = {
    "regime_n":   [30, 60, 120],          # consecutive days above the 200MA
    "bb_win":     [20, 50],
    "bb_dev":     [2.0, 2.5],
    "sar_step":   [0.01, 0.02],
    "sar_max":    [0.10, 0.20],
    "scale_frac": [0.33, 0.50, 1.00],     # 1.00 == full exit (no "core")
    "use_rsi":    [False, True],
    "short_leg":  [False, True],
}


# Indicator cache. SAR depends ONLY on (step, max_step); Bollinger only on
# (window, dev); the 200MA on nothing. Recomputing them inside the 576-combo
# loop meant `ta`'s PSAR — a pure-Python per-bar loop — ran 1,152 times over
# ~2,800 bars each. Keyed caching turns that into 4 SAR and 4 BB computations
# per market, which is the difference between minutes and hours.
_CACHE: dict = {}


def _sar(df, mkt, step, mx):
    k = ("sar", mkt, step, mx)
    if k not in _CACHE:
        from ta.trend import PSARIndicator
        _CACHE[k] = PSARIndicator(high=df["high"], low=df["low"],
                                  close=df["close"], step=step,
                                  max_step=mx).psar()
    return _CACHE[k]


def _bb(df, mkt, win, dev):
    k = ("bb", mkt, win, dev)
    if k not in _CACHE:
        from ta.volatility import BollingerBands
        _CACHE[k] = BollingerBands(close=df["close"], window=win,
                                   window_dev=dev).bollinger_hband()
    return _CACHE[k]


def _base(df, mkt):
    k = ("base", mkt)
    if k not in _CACHE:
        from ta.momentum import RSIIndicator
        sma = df["close"].rolling(200).mean()
        above = (df["close"] > sma).astype(int)
        grp = (above == 0).cumsum()
        _CACHE[k] = pd.DataFrame({
            "sma200": sma,
            "days_above": above.groupby(grp).cumsum(),
            "rsi": RSIIndicator(close=df["close"], window=14).rsi()},
            index=df.index)
    return _CACHE[k]


def indicators(df, p, mkt="_"):
    b = _base(df, mkt)
    d = pd.DataFrame({
        "close": df["close"],
        "bb_h": _bb(df, mkt, p["bb_win"], p["bb_dev"]),
        "sar": _sar(df, mkt, p["sar_step"], p["sar_max"]),
        "sma200": b["sma200"], "days_above": b["days_above"], "rsi": b["rsi"],
    }, index=df.index)
    d["sar_bull"] = d["sar"] < d["close"]
    return d


def simulate(d, p, fee):
    """Long-only core with partial scale-outs; optional counter-trend short."""
    trades = []
    pos = None                      # {"entry", "qty", "date"}
    short = None
    prev_bull = False
    for i in range(len(d)):
        r = d.iloc[i]
        if np.isnan(r["sma200"]) or np.isnan(r["bb_h"]) or np.isnan(r["sar"]):
            prev_bull = bool(r["sar_bull"]) if not np.isnan(r["sar"]) else prev_bull
            continue
        px, bull = float(r["close"]), bool(r["sar_bull"])
        prolonged = r["days_above"] >= p["regime_n"]
        at_band = px >= float(r["bb_h"])
        rsi_ok = (not p["use_rsi"]) or (r["rsi"] > 70)

        # --- manage short (counter-trend) ---
        if short is not None and (not bull or not prolonged):
            trades.append({"date": d.index[i], "pnl": (short["entry"] - px) / short["entry"] - 2 * fee,
                           "side": "short", "hold_candles": i - short["i"], "reason": "sar_flip"})
            short = None

        # --- manage long ---
        if pos is not None:
            if not bull:                                   # SAR flipped -> full exit
                trades.append({"date": d.index[i], "pnl": (px - pos["entry"]) / pos["entry"] * pos["qty"] - 2 * fee * pos["qty"],
                               "side": "long", "hold_candles": i - pos["i"], "reason": "sar_flip"})
                pos = None
            elif at_band and prolonged and rsi_ok and pos["qty"] > 0.01:
                # SCALE OUT: book FRAC of the remaining position at the band
                sold = pos["qty"] * p["scale_frac"]
                trades.append({"date": d.index[i], "pnl": (px - pos["entry"]) / pos["entry"] * sold - 2 * fee * sold,
                               "side": "long", "hold_candles": i - pos["i"], "reason": "scale_out"})
                pos["qty"] -= sold
                if pos["qty"] <= 0.01:
                    pos = None
                if p["short_leg"] and short is None:
                    short = {"entry": px, "i": i}

        # --- entry: SAR flips bullish inside a prolonged uptrend ---
        if pos is None and bull and not prev_bull and prolonged:
            pos = {"entry": px, "qty": 1.0, "i": i}
        prev_bull = bull
    return trades


def load_markets():
    from markets.adapters.crypto_binance import OfflineCryptoAdapter
    out = {}
    ad = OfflineCryptoAdapter(symbol="BTC/USDT:USDT", timeframe="15m")
    ad.connect()
    c = ad.all_candles()
    daily = pd.DataFrame({
        "open": c["open"].resample("1D").first(), "high": c["high"].resample("1D").max(),
        "low": c["low"].resample("1D").min(), "close": c["close"].resample("1D").last(),
        "volume": c["volume"].resample("1D").sum()}).dropna()
    daily.index = daily.index.tz_localize(None)
    out["BTC"] = daily

    from nse_cross_sectional import load_panel
    panel = load_panel("2015-01-01", "2026-09-11")
    liq = panel.groupby("symbol")["turnover"].median().nlargest(200).index
    sub = panel[panel["symbol"].isin(liq)]
    ohlc = sub.groupby("date").agg(open=("open", "mean"), high=("high", "mean"),
                                   low=("low", "mean"), close=("close", "mean"),
                                   volume=("volume", "sum"))
    out["NSE"] = ohlc.sort_index()
    return out


def main() -> int:
    mkts = load_markets()
    keys = list(GRID)
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    total = len(combos) * len(mkts)
    print(f"SAR + Bollinger scale-out grid\n  {len(combos)} parameter combos "
          f"x {len(mkts)} markets = {total} configurations\n")

    rows, pnl_by_cfg = [], {}
    for mkt, df in mkts.items():
        for c in combos:
            p = dict(zip(keys, c))
            d = indicators(df, p, mkt)
            tr = simulate(d, p, FEE[mkt])
            if len(tr) < 10:
                continue
            pn = np.array([t["pnl"] for t in tr])
            name = f"{mkt}|" + "|".join(f"{k}={p[k]}" for k in keys)
            rows.append({"market": mkt, "n": len(tr), "pf": profit_factor(pn),
                         "mean": pn.mean(), "total": pn.sum(), "cfg": name,
                         "trades": tr, **p})
            pnl_by_cfg[name] = list(pn)

    res = pd.DataFrame([{k: v for k, v in r.items() if k != "trades"} for r in rows])
    print(f"  {len(res)} configs produced >= 10 trades\n")
    print("  DISTRIBUTION OF PROFIT FACTOR ACROSS THE WHOLE GRID")
    print("  (the shape of this is the result — not the winner)")
    for m in mkts:
        s = res[res.market == m]["pf"]
        if len(s) == 0:
            continue
        print(f"    {m:<5} n={len(s):>4}  min {s.min():.2f}  p25 {s.quantile(.25):.2f}  "
              f"median {s.median():.2f}  p75 {s.quantile(.75):.2f}  max {s.max():.2f}  "
              f"| share PF>1: {(s > 1).mean():.0%}")

    print("\n  TOP 5 BY PROFIT FACTOR")
    for _, r in res.nlargest(5, "pf").iterrows():
        print(f"    PF {r['pf']:>5.2f}  n={r['n']:>4}  mean {r['mean']:>+7.3%}  "
              f"{r['cfg'][:88]}")

    best = res.nlargest(1, "pf").iloc[0]
    best_tr = next(r["trades"] for r in rows if r["cfg"] == best["cfg"])

    print(f"\n{'='*78}\n  THE WINNER, PRICED FOR THE SEARCH IT TOOK\n{'='*78}")
    g6 = deflated_sharpe([t["pnl"] for t in best_tr], n_trials=len(res))
    print(f"  gate 6  SR {g6['sr']:.3f} vs null-max SR0 {g6['sr0']:.3f} across "
          f"{len(res)} trials -> DSR {g6['dsr']:.4f}  "
          f"{'PASS' if g6['pass'] else 'FAIL'}")
    g7 = pbo(pnl_by_cfg, n_splits=8)
    if g7.get("pbo") is not None:
        print(f"  gate 7  PBO {g7['pbo']:.1%} over {g7['n_combos']} CSCV splits, "
              f"{g7['n_configs']} configs  {'PASS' if g7['pass'] else 'FAIL'}")
    else:
        print(f"  gate 7  {g7.get('note')}")
    run_gates(best_tr, label="BEST CONFIG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
