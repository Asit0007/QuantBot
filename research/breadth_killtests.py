"""
research/breadth_killtests.py — four attempts to destroy the breadth effect.

The effect (4 regions, 36y, n=418): a MACD cross in ONE market precedes
NEGATIVE forward returns; the same cross in ALL FOUR precedes positive ones.
p < 0.001 at 5/10/20d.

This is the first interesting result in the project, which is exactly when to
be most suspicious. Each test below is designed to FAIL the effect, not
confirm it. Order is cheapest-and-deadliest first.

  1 PLACEBO      shuffle the breadth labels. If the effect survives a
                 shuffle, the test itself is broken and nothing else matters.
  2 VENDOR       all four regions come from one Bloomberg pipeline. Swap
                 North America for the independent CRSP-based US series. A
                 shared processing artefact cannot survive a vendor change.
  3 SUB-PERIOD   does it hold in the 1990s, 2000s, 2010s and 2020s
                 separately, or is it one decade? This is gate 5's logic,
                 and it is what killed every BTC trend gate.
  4 TREND        THE MOST LIKELY KILLER. "All four markets crossed up" may
                 just mean "global uptrend", and buying uptrends is momentum,
                 not news. Compare b=4 against b=1 WITHIN matched trailing-
                 return buckets. If the effect vanishes once trend is
                 controlled, breadth adds nothing and the idea is dead.

Usage: ./venv/bin/python research/breadth_killtests.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cross_market import AGREE_WINDOW, macd_cross          # noqa: E402
from markets.adapters.us_french import load_region         # noqa: E402

RNG = np.random.default_rng(20260914)
HOR = [5, 10, 20]


def panel(regions):
    px = pd.DataFrame({r: load_region(r)["close"] for r in regions}).dropna()
    sig = pd.DataFrame({r: macd_cross(px[r]) for r in regions})
    near = sig.rolling(2 * AGREE_WINDOW + 1, center=True, min_periods=1).max().astype(bool)
    return px, sig, near.sum(axis=1)


def diff_at(px, sig, br, regions, h, hi_b, lo_b=1, mask_extra=None):
    hi, lo = [], []
    for r in regions:
        f = px[r].shift(-h) / px[r] - 1
        m = sig[r] if mask_extra is None else (sig[r] & mask_extra)
        hi.extend(f[m & (br == hi_b)].dropna().tolist())
        lo.extend(f[m & (br == lo_b)].dropna().tolist())
    return np.array(hi), np.array(lo)


def perm_p(hi, lo, n=4000):
    d = hi.mean() - lo.mean()
    pool = np.concatenate([hi, lo]); nh = len(hi)
    perm = np.array([(lambda q: q[:nh].mean() - q[nh:].mean())(RNG.permutation(pool))
                     for _ in range(n)])
    return d, float((np.abs(perm) >= abs(d)).mean())


def main() -> int:
    regions = ["EUROPE", "JAPAN", "ASIAPAC", "NORTHAM"]
    px, sig, br = panel(regions)
    print(f"baseline: {len(px):,} sessions {px.index[0].date()} -> {px.index[-1].date()}\n")

    # ── 1. PLACEBO ───────────────────────────────────────────────────
    print("1. PLACEBO — breadth labels shuffled. Effect MUST vanish.")
    for h in HOR:
        hi, lo = diff_at(px, sig, br, regions, h, 4)
        d_real, p_real = perm_p(hi, lo)
        shuffled = pd.Series(RNG.permutation(br.values), index=br.index)
        hs, ls = diff_at(px, sig, shuffled, regions, h, 4)
        d_fake = hs.mean() - ls.mean() if len(hs) > 5 and len(ls) > 5 else float("nan")
        print(f"   {h:>2}d  real {d_real:+.2%} (p={p_real:.3f})   "
              f"shuffled {d_fake:+.2%}   n_shuf={len(hs)}")

    # ── 2. VENDOR ────────────────────────────────────────────────────
    print("\n2. VENDOR — swap NORTHAM (Bloomberg) for US (CRSP, independent).")
    r2 = ["EUROPE", "JAPAN", "ASIAPAC", "US"]
    px2, sig2, br2 = panel(r2)
    print(f"   {len(px2):,} sessions")
    for h in HOR:
        hi, lo = diff_at(px2, sig2, br2, r2, h, 4)
        d, p = perm_p(hi, lo)
        print(f"   {h:>2}d  b=4 {hi.mean():+.2%}  b=1 {lo.mean():+.2%}  "
              f"diff {d:+.2%}  p={p:.3f}  n={len(hi)}/{len(lo)}")

    # ── 3. SUB-PERIOD ────────────────────────────────────────────────
    print("\n3. SUB-PERIOD — by decade. One decade carrying it = dead.")
    for lo_y, hi_y in [(1990, 1999), (2000, 2009), (2010, 2019), (2020, 2026)]:
        m = (px.index.year >= lo_y) & (px.index.year <= hi_y)
        sub_px, sub_sig, sub_br = px[m], sig[m], br[m]
        hi, lo = diff_at(sub_px, sub_sig, sub_br, regions, 10, 4)
        if len(hi) < 15 or len(lo) < 15:
            print(f"   {lo_y}s  n too small ({len(hi)}/{len(lo)})")
            continue
        d, p = perm_p(hi, lo, 2000)
        print(f"   {lo_y}-{hi_y}  10d: b=4 {hi.mean():+.2%}  b=1 {lo.mean():+.2%}  "
              f"diff {d:+.2%}  p={p:.3f}  n={len(hi)}/{len(lo)}")

    # ── 4. TREND CONTROL ─────────────────────────────────────────────
    print("\n4. TREND CONTROL — is breadth just 'global uptrend'?")
    print("   Comparing b=4 vs b=1 WITHIN matched trailing-60d-return terciles.")
    trail = {r: px[r] / px[r].shift(60) - 1 for r in regions}
    tr_df = pd.DataFrame(trail)
    for h in (5, 10):
        print(f"   --- {h}d ---")
        surviving = 0
        for q, lab in enumerate(["weak trend", "mid trend", "strong trend"]):
            hi, lo = [], []
            for r in regions:
                t = tr_df[r]
                edges = t.quantile([0.333, 0.667]).values
                bucket = (t > edges[0]).astype(int) + (t > edges[1]).astype(int)
                f = px[r].shift(-h) / px[r] - 1
                m = sig[r] & (bucket == q)
                hi.extend(f[m & (br == 4)].dropna().tolist())
                lo.extend(f[m & (br == 1)].dropna().tolist())
            hi, lo = np.array(hi), np.array(lo)
            if len(hi) < 10 or len(lo) < 10:
                print(f"     {lab:<14} n too small ({len(hi)}/{len(lo)})")
                continue
            d, p = perm_p(hi, lo, 2000)
            surviving += int(p < 0.05 and d > 0)
            print(f"     {lab:<14} b=4 {hi.mean():+.2%}  b=1 {lo.mean():+.2%}  "
                  f"diff {d:+.2%}  p={p:.3f}  n={len(hi)}/{len(lo)}")
        print(f"     -> {surviving}/3 terciles keep a significant positive effect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
