"""
Tests for the NSE bhavcopy adapter.

The one that matters most is test_universe_has_no_lookahead. Survivorship
bias is the classic way to manufacture a fake edge in cross-sectional
equity research, and its symptom is a backtest that looks great and cannot
be traded. build_universe() is the defence, so it needs a test that would
actually fail if someone "optimised" it into peeking at future data.

Runs entirely from the local cache — no network. Skips cleanly if the
cache has not been populated (research/fetch_nse_history.py).

Run: ./venv/bin/python tests/test_nse_bhavcopy.py
"""
import os
import sys
from datetime import date

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from markets.adapters.nse_bhavcopy import (   # noqa: E402
    COLUMNS, BhavcopyStore, NSEBhavcopyAdapter, build_universe)

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FAIL {msg}")
        FAILS.append(msg)


def main() -> int:
    store = BhavcopyStore()
    if not os.path.isdir(store.cache_dir) or not os.listdir(store.cache_dir):
        print("SKIP — bhavcopy cache is empty. "
              "Run research/fetch_nse_history.py first.")
        return 0

    cached = sorted(f[:-7] for f in os.listdir(store.cache_dir)
                    if f.endswith(".csv.gz"))
    print(f"cache: {len(cached)} sessions  {cached[0]} -> {cached[-1]}\n")

    # ── holiday vs missing ───────────────────────────────────────────
    print("holiday handling")
    check(store.is_session(date(2019, 9, 3)) is True, "2019-09-03 is a session")
    check(store.is_session(date(2019, 9, 2)) is False,
          "2019-09-02 (Ganesh Chaturthi) is not a session — matches NSE's own 404")
    check(store.day(date(2019, 9, 2)) is None, "holiday returns None, not an error")
    check(store.is_session(date(2026, 9, 12)) is False, "Saturday is not a session")

    # ── schema is identical across both file formats ─────────────────
    print("\nformat normalisation")
    panel = pd.concat(
        [pd.read_csv(os.path.join(store.cache_dir, f"{d}.csv.gz"), parse_dates=["date"])
         for d in cached], ignore_index=True)
    check(list(panel.columns) == COLUMNS, f"schema is {COLUMNS}")
    check(panel["close"].notna().all(), "no NaN closes survive normalisation")
    check((panel["close"] > 0).all(), "all closes positive")
    check(panel["isin"].str.startswith("INE").mean() > 0.8,
          "ISINs look like Indian ISINs (>80% INE-prefixed)")

    # ── THE IMPORTANT ONE ────────────────────────────────────────────
    print("\nsurvivorship / lookahead")
    mid = pd.Timestamp(cached[len(cached) // 2])
    full = build_universe(panel, mid, top_n=20)
    truncated = build_universe(panel[panel["date"] <= mid], mid, top_n=20)
    check(full == truncated,
          "universe @T is identical whether or not post-T data exists "
          "(build_universe cannot see the future)")

    early, late = pd.Timestamp(cached[len(cached) // 4]), pd.Timestamp(cached[-1])
    ue, ul = build_universe(panel, early, top_n=50), build_universe(panel, late, top_n=50)
    check(ue != ul,
          "universe CHANGES over time — a constant list would mean the "
          "point-in-time reconstruction is not actually happening")
    check(len(ul) == 50, "returns the requested size")

    # ── adapter surface ──────────────────────────────────────────────
    print("\nadapter")
    ad = NSEBhavcopyAdapter(store=store, panel=panel)
    f = ad.symbol_frame("RELIANCE")
    check(list(f.columns) == ["open", "high", "low", "close", "volume"],
          "symbol_frame gives the engine's OHLCV shape")
    check(f.index.is_monotonic_increasing, "symbol frame is sorted ascending")
    check(f.index.is_unique, "one bar per date")
    check((f["high"] >= f["low"]).all(), "high >= low on every bar")
    check((f["high"] >= f["close"]).all() and (f["low"] <= f["close"]).all(),
          "close sits within the bar's range")
    spec = ad.instrument("RELIANCE")
    check(spec.lot_step == 1.0 and spec.quote_ccy == "INR",
          "NSE equities are whole shares priced in INR")

    # get_position must raise, never return None — see the adapter docstring.
    try:
        ad.get_position("RELIANCE")
        check(False, "get_position raises rather than claiming 'flat'")
    except NotImplementedError:
        check(True, "get_position raises rather than claiming 'flat'")

    print(f"\n{'PASS' if not FAILS else 'FAIL'} — {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
