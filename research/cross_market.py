"""
╔══════════════════════════════════════════════════════════════════════╗
║  research/cross_market.py — does SIGNAL BREADTH across markets        ║
║  predict better than any single market's signal?                      ║
╠══════════════════════════════════════════════════════════════════════╣
║  THE IDEA. This project has been treating multi-market as "more       ║
║  trades". It is worth more than that: it is a multiple-testing        ║
║  defence, and it is nearly free.                                      ║
║                                                                      ║
║  Running N configs on BTC, then N on NSE, then N on US gives three    ║
║  independent chances to fool yourself and the trial count keeps       ║
║  climbing — which is exactly how ~100 BTC configs made test #101 less ║
║  trustworthy than test #1. Invert it: take ONE signal, run it         ║
║  UNCHANGED on three uncorrelated markets, and require agreement.      ║
║                                                                      ║
║  If a spurious signal clears one market with probability p, clearing  ║
║  three uncorrelated ones is p³. At p = 0.05 that is 0.000125 — a      ║
║  ~400x stronger filter at ZERO additional degrees of freedom, because ║
║  nothing is tuned per market.                                         ║
║                                                                      ║
║  THE TRADEABLE VERSION. When the same setup fires in BTC, NSE and US  ║
║  at once, that is plausibly one global risk-on event rather than      ║
║  three local coincidences. So: does BREADTH (how many markets agree)  ║
║  predict forward returns better than the signal alone?                ║
║                                                                      ║
║  ONE signal only — MACD bullish cross. Standard, close-only so it     ║
║  computes identically on all three, and NOT chosen by searching.      ║
║  Adding a second signal here would be spending degrees of freedom on  ║
║  the very test built to conserve them.                                ║
║                                                                      ║
║  HONEST LIMITS, stated before the numbers:                            ║
║   * The three-way window is bounded by BTC (2019-09), so ~7 years.    ║
║     The NSE+US pair runs longer and is reported separately.           ║
║   * US is the Ken French total-market series: close-only, no OHLC.    ║
║     That is why the signal must be close-only.                        ║
║   * Forward windows overlap, so t-stats here are optimistic. This is  ║
║     a screen, not a proof.                                            ║
╚══════════════════════════════════════════════════════════════════════╝

Usage: ./venv/bin/python research/cross_market.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from markets.adapters.crypto_binance import OfflineCryptoAdapter   # noqa: E402
from markets.adapters.us_french import load_us_market              # noqa: E402
from nse_cross_sectional import load_panel                         # noqa: E402

HORIZONS = [5, 10, 20, 40]

# Agreement window, in trading days.
#
# The first version of this test required the signal to fire on the SAME
# CALENDAR DAY in every market, and found only 7 such days in 2,763 — n=14
# observations, from which nothing can be concluded. That is a measurement
# artefact, not evidence about the hypothesis: a MACD cross is a single-day
# point event, and three markets on different calendars and time zones will
# essentially never produce one on the same date.
#
# So agreement is measured as "fired within +/- AGREE_WINDOW days". Widening
# it IS a researcher degree of freedom, so it is pre-committed at 3 days,
# tested once, and counted as a trial. It is NOT swept.
AGREE_WINDOW = 3


def macd_cross(close: pd.Series) -> pd.Series:
    """Standard MACD(12,26,9) bullish cross. Close-only, no parameters tuned."""
    ef = close.ewm(span=12, adjust=False).mean()
    es = close.ewm(span=26, adjust=False).mean()
    line = ef - es
    sig = line.ewm(span=9, adjust=False).mean()
    return (line > sig) & (line.shift(1) <= sig.shift(1))


def build_series() -> dict[str, pd.Series]:
    out = {}

    ad = OfflineCryptoAdapter(symbol="BTC/USDT:USDT", timeframe="15m")
    ad.connect()
    btc = ad.all_candles()["close"].resample("1D").last().dropna()
    btc.index = btc.index.tz_localize(None).normalize()
    out["BTC"] = btc

    panel = load_panel("2015-01-01", "2026-09-11")
    # Equal-weight index of the liquid universe: mean daily return across the
    # 200 most-traded symbols, compounded. Equal-weight rather than
    # cap-weighted because bhavcopy has no share count — and stating that is
    # better than pretending to a cap weighting it cannot compute.
    liq = panel.groupby("symbol")["turnover"].median().nlargest(200).index
    px = (panel[panel["symbol"].isin(liq)]
          .pivot_table(index="date", columns="symbol", values="close"))
    ret = px.pct_change().clip(-0.25, 0.25)      # clip residual split artefacts
    nse = 100 * (1 + ret.mean(axis=1).fillna(0)).cumprod()
    nse.index = pd.to_datetime(nse.index).normalize()
    out["NSE"] = nse

    us = load_us_market()["close"]
    us.index = pd.to_datetime(us.index).normalize()
    out["US"] = us
    return out


def breadth_table(series: dict[str, pd.Series], names: list[str], label: str):
    idx = series[names[0]].index
    for n in names[1:]:
        idx = idx.intersection(series[n].index)
    if len(idx) < 300:
        print(f"\n  {label}: only {len(idx)} common sessions — skipping")
        return
    px = pd.DataFrame({n: series[n].reindex(idx) for n in names}).dropna()
    sig = pd.DataFrame({n: macd_cross(px[n]) for n in names})
    # "Fired recently" rather than "fired today" — see AGREE_WINDOW.
    # TRAILING, never centred. A centred window marks a bar as "signal fired
    # recently" up to AGREE_WINDOW days BEFORE the signal exists — lookahead
    # bias, and the exact class of bug that once produced a $2.4bn backtest in
    # this repo. Trailing means breadth at time t uses only t-6..t.
    near = sig.rolling(2 * AGREE_WINDOW + 1, min_periods=1).max().astype(bool)
    breadth = near.sum(axis=1)

    yrs = (px.index[-1] - px.index[0]).days / 365.25
    print(f"\n  ── {label} ── {len(px):,} common sessions, "
          f"{px.index[0].date()} -> {px.index[-1].date()} ({yrs:.1f}y)")
    print(f"     signal days by breadth: "
          f"{ {int(k): int(v) for k, v in breadth.value_counts().sort_index().items()} }")

    for h in HORIZONS:
        fwd = {n: px[n].shift(-h) / px[n] - 1 for n in names}
        line = f"     {h:>2}d  "
        for b in range(1, len(names) + 1):
            vals = []
            for n in names:
                m = sig[n] & (breadth == b)
                v = fwd[n][m].dropna()
                vals.extend(v.tolist())
            if len(vals) >= 10:
                a = np.array(vals)
                line += f"b={b}: {a.mean():+.2%} (n={len(a):>4})  "
            else:
                line += f"b={b}: n<10          "
        base = np.concatenate([(fwd[n].dropna()).to_numpy() for n in names])
        line += f"| base {base.mean():+.2%}"
        print(line)


def main() -> int:
    print("CROSS-MARKET BREADTH TEST — one signal (MACD bull cross), "
          "three markets, nothing tuned per market")
    s = build_series()
    for k, v in s.items():
        print(f"  {k:<4} {len(v):>7,} sessions  {v.index[0].date()} -> {v.index[-1].date()}")
    breadth_table(s, ["NSE", "US"], "PAIR: NSE + US (longer window)")
    breadth_table(s, ["BTC", "NSE", "US"], "TRIPLE: BTC + NSE + US")
    print("\n  Read: if breadth matters, b=2/3 columns should beat b=1 AND the")
    print("  baseline. If they do not, cross-market agreement carries no extra")
    print("  information and the idea is dead — which is a result worth having.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
