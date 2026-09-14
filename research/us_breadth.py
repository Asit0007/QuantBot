"""
research/us_breadth.py — the breadth strategy on the US market.

CLEAN OUT-OF-SAMPLE DESIGN. North America was one of the four regions used to
DISCOVER the breadth effect, so testing on it would be in-sample and worth
little. Breadth is therefore computed from EUROPE + JAPAN + ASIA-PACIFIC only
(max breadth = 3), and the US is the untouched market being predicted.

The US series here is the CRSP-based Ken French total-market return — a
different vendor and a different construction from the three Bloomberg-based
regions driving the signal, which also rules out a shared-pipeline artefact.

ON SPY SPECIFICALLY: the US total market and the S&P 500 correlate ~0.99 daily,
so this validates the SIGNAL. Trading SPY itself needs real OHLC (for a stop),
which every free source walls behind a key.

Costs: 10bps round trip — SPY is among the most liquid instruments on earth,
one cent on a ~$600 price, plus commission-free retail execution.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cross_market import AGREE_WINDOW, macd_cross                 # noqa: E402
from markets.adapters.us_french import load_region                # noqa: E402
from overfitting import run_extended                              # noqa: E402
from robustness import run_gates                                  # noqa: E402

SIGNAL_REGIONS = ["EUROPE", "JAPAN", "ASIAPAC"]     # NOT North America
HOLD = 10
COST = 10 / 10_000
TRIALS = 12          # every config spent on the breadth idea, this one included
RNG = np.random.default_rng(20260914)


def main() -> int:
    sig_px = pd.DataFrame({r: load_region(r)["close"] for r in SIGNAL_REGIONS}).dropna()
    sig = pd.DataFrame({r: macd_cross(sig_px[r]) for r in SIGNAL_REGIONS})
    near = sig.rolling(2 * AGREE_WINDOW + 1, center=True, min_periods=1).max().astype(bool)
    br = near.sum(axis=1)

    us = load_region("US")["close"]
    idx = us.index.intersection(br.index)
    px, b = us.reindex(idx), br.reindex(idx)
    yrs = (idx[-1] - idx[0]).days / 365.25
    print(f"US breadth test — signal from {SIGNAL_REGIONS} (North America EXCLUDED)")
    print(f"  {len(idx):,} sessions  {idx[0].date()} -> {idx[-1].date()}  ({yrs:.1f}y)\n")

    # ── 1. does it predict at all? ───────────────────────────────────
    print("  FORWARD RETURNS BY BREADTH")
    print(f"  {'hor':>4}{'all 3':>10}{'one/none':>11}{'diff':>9}{'perm p':>9}{'n(3)':>7}")
    for h in (5, 10, 20):
        f = px.shift(-h) / px - 1
        hi = f[b == 3].dropna().to_numpy()
        lo = f[b <= 1].dropna().to_numpy()
        d = hi.mean() - lo.mean()
        pool = np.concatenate([hi, lo]); nh = len(hi)
        perm = np.array([(lambda q: q[:nh].mean() - q[nh:].mean())(RNG.permutation(pool))
                         for _ in range(4000)])
        p = float((np.abs(perm) >= abs(d)).mean())
        print(f"  {h:>3}d{hi.mean():>+9.2%}{lo.mean():>+11.2%}{d:>+9.2%}{p:>9.3f}{len(hi):>7}")

    # ── 2. as a tradeable book ───────────────────────────────────────
    trades, i, n = [], 0, len(idx)
    while i < n - HOLD:
        if b.iloc[i] == 3:
            e, x = float(px.iloc[i]), float(px.iloc[i + HOLD])
            trades.append({"date": idx[i + HOLD], "entry_date": idx[i],
                           "market": "US", "pnl": x / e - 1 - COST,
                           "gross": x / e - 1, "hold_candles": HOLD, "reason": "time"})
            i += HOLD
        else:
            i += 1

    p = np.array([t["pnl"] for t in trades])
    wins = p[p > 0]
    gl = -p[p <= 0].sum()
    pf = (wins.sum() / gl) if gl > 0 else float("inf")
    eq = np.cumprod(1 + p)
    dd = float((1 - eq / np.maximum.accumulate(eq)).max())
    bh = float(px.iloc[-1] / px.iloc[0])
    expo = len(trades) * HOLD / len(idx)
    t_yrs = (pd.Timestamp(trades[-1]["date"]) - pd.Timestamp(trades[0]["entry_date"])).days / 365.25

    print(f"\n  TRADEABLE BOOK — {HOLD}d hold, {COST*10000:.0f}bps round trip")
    print(f"    trades {len(trades)}   WR {100*len(wins)/len(p):.1f}%   PF {pf:.2f}   "
          f"mean/trade {p.mean():+.2%}")
    print(f"    equity {eq[-1]:.2f}x over {t_yrs:.1f}y   CAGR {eq[-1]**(1/t_yrs)-1:.1%}   "
          f"maxDD {dd:.1%}")
    print(f"    exposure {expo:.0%} of the time   |   buy & hold {bh:.2f}x")
    # Random timing at the same exposure is the fair comparison to timing skill.
    print(f"    buy & hold scaled to that exposure ~ {bh**expo:.2f}x  "
          f"<- the bar timing skill must clear")

    print("\n" + "=" * 74)
    run_gates(trades, label="US BREADTH")
    run_extended(trades, n_trials=TRIALS, label="US BREADTH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
