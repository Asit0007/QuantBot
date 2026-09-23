"""
research/fetch_nse_history.py — populate the local NSE bhavcopy cache.

One request per TRADING DAY, each covering the whole market. Files are
static once published, so this is a one-off crawl: everything lands in
data_cache/nse_bhavcopy/<date>.csv.gz and is never re-fetched.

Politeness is deliberate (see REQUEST_DELAY_S in the adapter). This is a
free public archive run by an exchange, and a one-off slow crawl costs us
nothing while hammering it would be rude and would likely get the IP
throttled anyway.

Usage:
    ./venv/bin/python research/fetch_nse_history.py 2015-01-01 2026-09-12
"""
from __future__ import annotations

import sys
import time
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from markets.adapters.nse_bhavcopy import BhavcopyStore   # noqa: E402


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "2015-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-12"
    t0 = time.time()
    store = BhavcopyStore()
    print(f"fetching NSE bhavcopy {start} -> {end}", flush=True)
    panel = store.load_range(start, end, progress_every=100)
    mins = (time.time() - t0) / 60
    if panel.empty:
        print("no data")
        return 1
    print(f"\ndone in {mins:.1f} min")
    print(f"  rows      {len(panel):,}")
    print(f"  symbols   {panel['symbol'].nunique():,}")
    print(f"  sessions  {panel['date'].nunique():,}")
    print(f"  range     {panel['date'].min().date()} -> {panel['date'].max().date()}")
    cache = store.cache_dir
    n = len(os.listdir(cache))
    mb = sum(os.path.getsize(os.path.join(cache, f)) for f in os.listdir(cache)) / 1e6
    print(f"  cache     {n:,} files, {mb:,.0f} MB in {cache}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
