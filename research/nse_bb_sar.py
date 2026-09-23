"""
research/nse_bb_sar.py — CONFIG #2: squeeze-break (Bollinger + Parabolic SAR).

⚠️ This family is ALREADY REJECTED on crypto — backtest/README.md records
"BB + Parabolic SAR (± RSI) across 5m-4h: 0/20 with bootstrap p5 > 1".
It is tested here only because NSE equities are a market where it has never
been tried, and because it fixes config #1's DIAGNOSED failure: config #1's
exit fires on ~0.02% of daily bars, so the ATR stop became the only way out
and the win rate was 6.2% by construction. SAR always eventually exits.

Scored on all EIGHT gates. Counts as one more trial against NSE data.

Usage: ./venv/bin/python research/nse_bb_sar.py [start] [end]
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from markets.strategy import compute_bb_sar                       # noqa: E402
from nse_cross_sectional import (load_panel, nse_config,          # noqa: E402
                                 run_study, summarise)
from overfitting import run_extended                              # noqa: E402
from robustness import run_gates                                  # noqa: E402

# configs 1, 1b, 1c, and now 2. Increment for every variant, discarded ones
# included — this is the input gate 6 depends on and the easiest one to fudge.
NSE_TRIALS = 4


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "2015-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-11"
    print("NSE CONFIG #2 — squeeze-break: BB(20,2) regime filter + SAR exit")
    print("long only, 1x, ATR stop underneath, top-50 by turnover\n")

    panel = load_panel(start, end)
    print(f"  panel {len(panel):,} rows, {panel['date'].nunique():,} sessions")
    cfg = nse_config()
    trades, st = run_study(panel, cfg, indicator_fn=compute_bb_sar)
    summarise(trades, st, "CONFIG #2 (squeeze-break)")
    run_gates(trades, label="CONFIG #2")
    run_extended(trades, n_trials=NSE_TRIALS, label="CONFIG #2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
