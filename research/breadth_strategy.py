"""
research/breadth_strategy.py — the breadth effect as a tradeable strategy,
with real costs, scored on all eight gates.

DESIGN, AND WHY IT IS SHAPED THIS WAY
-------------------------------------
Signal:  breadth == 4, computed ONLY from the four Ken French regions.
Traded:  NSE index and BTC — the two markets that played NO part in
         discovering the effect.

That split is the point. The strategy never trades a market that was used to
find the signal, so the whole backtest is out-of-sample by construction. The
alternative — trading the same four regions the effect was discovered in —
would be in-sample and worth much less.

Entry:   long at the close on the first b=4 day while flat.
Exit:    after HOLD_DAYS trading days, at the close.
         A time exit, not a tuned one: every test showed the effect peaks at
         5-10 days and decays by 20, so 10 is read off the measured decay
         rather than searched for. No stop — adding one is another parameter
         and another trial; its absence is recorded as a limitation below.

COSTS ARE THE WHOLE QUESTION. The effect is ~1-3% over 10 days. This project
has already watched a beautiful-looking 603-trade strategy with a 42.8% win
rate die once fees were real (config #2). So costs are charged per side, per
market, and swept.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cross_market import AGREE_WINDOW, build_series, macd_cross    # noqa: E402
from markets.adapters.us_french import load_region                 # noqa: E402
from overfitting import run_extended                               # noqa: E402
from robustness import run_gates                                   # noqa: E402

REGIONS = ["EUROPE", "JAPAN", "ASIAPAC", "NORTHAM"]
HOLD_DAYS = 10

# Round-trip cost in basis points, per market. Deliberately generous.
#   NSE index ETF: brokerage + STT + stamp duty + exchange + GST, plus
#     slippage on a liquid ETF.
#   BTC: 5bp taker per side on Binance = 10bp round trip, plus slippage.
COSTS_BPS = {"NSE": 30.0, "BTC": 20.0}

# Every configuration spent on this idea: same-day, +/-3d pair, +/-3d triple,
# 4-region, vendor swap, sub-period, trend control, placebo, OOS, and this
# strategy. Kill-tests could only ever have falsified, never selected, but
# they are counted anyway — being generous here makes gate 6 harsher, which
# is the correct direction to err.
TRIALS = 10


def breadth_series() -> pd.Series:
    px = pd.DataFrame({r: load_region(r)["close"] for r in REGIONS}).dropna()
    sig = pd.DataFrame({r: macd_cross(px[r]) for r in REGIONS})
    near = sig.rolling(2 * AGREE_WINDOW + 1, min_periods=1).max().astype(bool)
    return near.sum(axis=1)


def backtest(price: pd.Series, breadth: pd.Series, cost_bps: float,
             market: str, hold: int = HOLD_DAYS) -> list[dict]:
    idx = price.index.intersection(breadth.index)
    p, b = price.reindex(idx), breadth.reindex(idx)
    trades, i, n = [], 0, len(idx)
    cost = cost_bps / 10_000.0
    while i < n - hold:
        if b.iloc[i] == 4:
            entry, exit_ = p.iloc[i], p.iloc[i + hold]
            gross = exit_ / entry - 1.0
            net = gross - cost                    # round trip, charged once
            trades.append({
                "date": idx[i + hold], "entry_date": idx[i], "market": market,
                "entry": float(entry), "exit": float(exit_),
                "gross_pct": float(gross), "pnl": float(net),
                "hold_candles": hold, "reason": "time",
            })
            i += hold                             # no pyramiding, no overlap
        else:
            i += 1
    return trades


def summarise(trades, label):
    if not trades:
        print(f"  {label}: no trades")
        return
    p = np.array([t["pnl"] for t in trades])
    g = np.array([t["gross_pct"] for t in trades])
    wins = p[p > 0]
    gl = -p[p <= 0].sum()
    pf = (wins.sum() / gl) if gl > 0 else float("inf")
    # CAPITAL SPLIT ACROSS MARKETS. My first version ran cumprod over the
    # interleaved trades of both books, which treats them as sequential
    # ALL-IN bets of the full account and overstated the result by ~4x
    # (18.60x vs a correct 4.70x; CAGR 29.4% vs 14.6%). Each market gets
    # 1/n of capital, so a trade moves the book by pnl/n.
    n_books = len({t["market"] for t in trades})
    eq = np.cumprod(1 + p / n_books)
    dd = float((1 - eq / np.maximum.accumulate(eq)).max())
    yrs = (pd.Timestamp(trades[-1]["date"]) - pd.Timestamp(trades[0]["entry_date"])).days / 365.25
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    print(f"  {label:<22}{len(trades):>7}{100*len(wins)/len(p):>8.1f}%{pf:>8.2f}"
          f"{g.mean():>9.2%}{p.mean():>9.2%}{eq[-1]:>9.2f}x{cagr:>8.1%}{dd:>8.1%}")


def main() -> int:
    br = breadth_series()
    other = build_series()
    prices = {}
    for m in ("NSE", "BTC"):
        s = other[m]
        s.index = pd.to_datetime(s.index).normalize()
        prices[m] = s

    print(f"BREADTH STRATEGY — signal from {REGIONS}, traded on NSE + BTC "
          f"(both out-of-sample)\n  hold {HOLD_DAYS}d, costs "
          f"{COSTS_BPS} bps round trip\n")
    print(f"  {'market':<22}{'trades':>7}{'WR':>9}{'PF':>8}{'gross':>9}"
          f"{'net':>9}{'equity':>10}{'CAGR':>8}{'maxDD':>8}")
    print("  " + "-" * 90)

    all_trades = []
    for m in ("NSE", "BTC"):
        tr = backtest(prices[m], br, COSTS_BPS[m], m)
        summarise(tr, m)
        all_trades.extend(tr)
    combined = sorted(all_trades, key=lambda t: pd.Timestamp(t["date"]))
    summarise(combined, "COMBINED")

    # ── cost sensitivity: the effect is ~1-3% per trade, so this matters ──
    print(f"\n  Cost sensitivity (round-trip bps applied to BOTH markets):")
    print(f"  {'bps':>6}{'trades':>8}{'net/trade':>11}{'PF':>8}{'equity':>9}")
    for bps in (0, 10, 20, 30, 50, 80, 120):
        t = []
        for m in ("NSE", "BTC"):
            t.extend(backtest(prices[m], br, bps, m))
        p = np.array([x["pnl"] for x in t])
        gl = -p[p <= 0].sum()
        pf = (p[p > 0].sum() / gl) if gl > 0 else float("inf")
        print(f"  {bps:>6}{len(t):>8}{p.mean():>11.2%}{pf:>8.2f}"
              f"{np.cumprod(1+p)[-1]:>8.2f}x")

    print("\n" + "=" * 78)
    print("  ALL EIGHT GATES — combined book")
    print("=" * 78)
    run_gates(combined, label="BREADTH STRATEGY")
    run_extended(combined, n_trials=TRIALS, label="BREADTH STRATEGY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
