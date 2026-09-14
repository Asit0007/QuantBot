"""
research/breadth_oos.py — the two decisive tests.

A. PLACEBO, done properly. My first attempt was a SINGLE shuffle draw with
   n~105 against the real 418 — uninterpretable. This runs thousands of
   CIRCULAR SHIFTS of the breadth series: a shift preserves the full
   autocorrelation structure of breadth AND of returns while destroying the
   alignment between them, which is the right null for "does breadth line up
   with future returns beyond chance?" A plain shuffle would also destroy the
   serial structure, making the null easier to beat than it should be.

B. OUT-OF-SAMPLE. The effect was discovered on four Ken French regions.
   NSE and BTC played no part in finding it. So: compute breadth ONLY from
   those four regions, then ask whether it predicts INDIAN EQUITY and BITCOIN
   returns — markets the signal has never seen. That is genuine out-of-sample
   transfer, not another in-sample slice.

   If a global-breadth signal built from Europe/Japan/Asia-Pacific/North
   America predicts Indian stocks and Bitcoin, it is measuring something real
   about global risk appetite. If it does not, the effect is local to the
   regions it was found in and far less interesting.

Usage: ./venv/bin/python research/breadth_oos.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cross_market import AGREE_WINDOW, build_series, macd_cross   # noqa: E402
from markets.adapters.us_french import load_region                # noqa: E402

RNG = np.random.default_rng(20260914)
REGIONS = ["EUROPE", "JAPAN", "ASIAPAC", "NORTHAM"]
N_SHIFT = 3000


def region_breadth():
    px = pd.DataFrame({r: load_region(r)["close"] for r in REGIONS}).dropna()
    sig = pd.DataFrame({r: macd_cross(px[r]) for r in REGIONS})
    near = sig.rolling(2 * AGREE_WINDOW + 1, center=True, min_periods=1).max().astype(bool)
    return px, sig, near.sum(axis=1)


def effect(px, sig, br, h, regions=REGIONS):
    hi, lo = [], []
    for r in regions:
        f = px[r].shift(-h) / px[r] - 1
        hi.extend(f[sig[r] & (br == 4)].dropna().tolist())
        lo.extend(f[sig[r] & (br == 1)].dropna().tolist())
    if len(hi) < 10 or len(lo) < 10:
        return np.nan, len(hi), len(lo)
    return np.mean(hi) - np.mean(lo), len(hi), len(lo)


def placebo():
    px, sig, br = region_breadth()
    print("A. PLACEBO — 3,000 circular shifts of the breadth series")
    print("   (a shift keeps both series' autocorrelation, kills only the alignment)\n")
    n = len(br)
    for h in (5, 10, 20):
        real, nh, nl = effect(px, sig, br, h)
        null = np.empty(N_SHIFT)
        vals = br.values
        for i in range(N_SHIFT):
            k = RNG.integers(60, n - 60)
            shifted = pd.Series(np.roll(vals, k), index=br.index)
            d, a, b = effect(px, sig, shifted, h)
            null[i] = d
        null = null[~np.isnan(null)]
        pct = float((null < real).mean() * 100)
        p = float((np.abs(null) >= abs(real)).mean())
        print(f"   {h:>2}d  real {real:+.2%}   null mean {null.mean():+.2%} "
              f"sd {null.std():.2%}   real sits at {pct:.1f}th pct   p={p:.4f}")
    print()


def out_of_sample():
    print("B. OUT-OF-SAMPLE — breadth from the 4 regions ONLY, tested on")
    print("   markets that played no part in discovering it\n")
    px, sig, br = region_breadth()
    other = build_series()          # BTC, NSE, US

    for name in ("NSE", "BTC"):
        s = other[name]
        s.index = pd.to_datetime(s.index).normalize()
        idx = s.index.intersection(br.index)
        if len(idx) < 300:
            print(f"   {name}: only {len(idx)} overlapping sessions — skip")
            continue
        b = br.reindex(idx)
        p = s.reindex(idx)
        yrs = (idx[-1] - idx[0]).days / 365.25
        print(f"   {name}: {len(idx):,} sessions {idx[0].date()} -> {idx[-1].date()} "
              f"({yrs:.1f}y)")
        print(f"        {'hor':>4}{'b=4':>9}{'b<=1':>9}{'diff':>9}{'p':>8}"
              f"{'n(4)':>7}{'n(<=1)':>8}")
        for h in (5, 10, 20):
            f = p.shift(-h) / p - 1
            hi = f[b == 4].dropna().to_numpy()
            lo = f[b <= 1].dropna().to_numpy()
            if len(hi) < 10 or len(lo) < 10:
                print(f"        {h:>3}d  n too small")
                continue
            d = hi.mean() - lo.mean()
            pool = np.concatenate([hi, lo]); nh = len(hi)
            perm = np.array([(lambda q: q[:nh].mean() - q[nh:].mean())(RNG.permutation(pool))
                             for _ in range(4000)])
            pv = float((np.abs(perm) >= abs(d)).mean())
            print(f"        {h:>3}d{hi.mean():>+8.2%}{lo.mean():>+9.2%}{d:>+9.2%}"
                  f"{pv:>8.3f}{len(hi):>7}{len(lo):>8}")
        print()


def main() -> int:
    placebo()
    out_of_sample()
    print("   NOTE: breadth here is computed WITHOUT NSE or BTC, so these two")
    print("   markets are genuine out-of-sample. Forward windows still overlap,")
    print("   so p remains a floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
