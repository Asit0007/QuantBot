"""
research/constant_notional.py — hold EXPOSURE constant, vary only liquidation risk.

THE OBSERVATION NOBODY HAS TESTED HERE. Under nostop sizing:

    notional / corpus = risk_per_trade x leverage

Production is 0.10 x 5 = 0.50. But so is 0.50 x 1, and 0.25 x 2, and 0.05 x 10.
Every one of those takes the SAME dollar exposure to BTC and therefore the same
P&L per unit of price move. What differs is only how far away liquidation sits.

The repo's existing leverage sweep varied leverage at FIXED risk_per_trade,
which changes notional at the same time — so it measured leverage and position
size together and could not separate them. This isolates the variable.

WHY IT MIGHT MATTER: the production config liquidates on 13 of 127 trades
(10.2%), and the log records PF decaying 1.96 -> 1.60 -> 1.42 as liquidations
climb 0% -> 10% -> 31%. If identical exposure can be had with fewer forced
exits, those liquidations are pure deadweight.

WHAT IT COSTS: at 1x the margin posted IS the notional, so 50% of corpus is
committed rather than 10%. The tail is also different — a 5x liquidation caps
the loss at the 10% margin, while 1x rides the position down. That trade is the
thing being measured, not assumed.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_manager import CorpusManager                            # noqa: E402
from markets.adapters.crypto_binance import OfflineCryptoAdapter    # noqa: E402
from markets.engine import MarketEngine                             # noqa: E402
from markets.state import new_state                                 # noqa: E402
from markets.strategy import compute_indicators                     # noqa: E402
from replay_btc import (CORPUS_REFRESH, INITIAL_BALANCE,            # noqa: E402
                        SYMBOL, build_config)
from robustness import bootstrap_pf, profit_factor, run_gates       # noqa: E402

# (leverage, risk_per_trade) — every pair gives notional = 0.50 x corpus
VARIANTS = [(1.0, 0.50), (2.0, 0.25), (3.0, 0.1667), (5.0, 0.10), (10.0, 0.05)]


def run(cfg, ad, df):
    cm = CorpusManager(initial_balance=INITIAL_BALANCE,
                       base_monthly_dca=cfg.dca_monthly,
                       dca_annual_growth=cfg.dca_annual_growth,
                       ratchet_up_every=CORPUS_REFRESH,
                       ratchet_down_after=CORPUS_REFRESH)
    st = new_state(INITIAL_BALANCE)
    tr = []
    eng = MarketEngine(cfg, ad, cm, st, exit_model="nostop", paper=True,
                       on_trade=tr.append)
    for n, (ts, row) in enumerate(df.iterrows()):
        eng.process_candle(SYMBOL, row, n, ts.to_pydatetime())
        eng.end_of_bar()
    return tr, st


def main() -> int:
    base = build_config()
    ad = OfflineCryptoAdapter(symbol=SYMBOL, timeframe="15m")
    ad.connect()
    df = compute_indicators(ad.all_candles().iloc[31:].copy(), base)

    print("CONSTANT NOTIONAL — every row takes the SAME dollar exposure (0.50 x corpus)")
    print("Only the distance to liquidation changes.\n")
    print(f"  {'lev':>4}{'risk':>7}{'liq dist':>10}{'trades':>8}{'WR':>8}{'PF':>7}"
          f"{'p5':>7}{'final $':>10}{'liq':>5}{'maxDD':>8}")
    print("  " + "-" * 76)

    keep = {}
    for lev, risk in VARIANTS:
        cfg = replace(base, leverage=lev, risk_per_trade=risk)
        tr, st = run(cfg, ad, df)
        if not tr:
            continue
        p = np.array([t["pnl"] for t in tr])
        w = p[p > 0]
        liq = sum(1 for t in tr if t["reason"] == "liquidated")
        p5, _, _, _ = bootstrap_pf(p, n=4000)
        bal = np.concatenate([[INITIAL_BALANCE], np.array([t["balance"] for t in tr])])
        dd = float((1 - bal / np.maximum.accumulate(bal)).max())
        liq_dist = (1.0 / lev - base.maint_margin_rate) * 100
        tag = "  <- production" if lev == 5.0 else ""
        print(f"  {lev:>4.0f}x{risk:>7.0%}{liq_dist:>9.1f}%{len(tr):>8}"
              f"{100*len(w)/len(p):>7.1f}%{profit_factor(p):>7.2f}{p5:>7.2f}"
              f"{st['balance']:>10,.0f}{liq:>5}{dd:>8.1%}{tag}")
        keep[lev] = tr

    print("\n  Same exposure, same signal, same trades entered. The ONLY difference")
    print("  is how often a position is force-closed by the exchange.")

    if 1.0 in keep:
        print("\n" + "=" * 74)
        print("  GATES — 1x variant (identical exposure, zero liquidation risk)")
        print("=" * 74)
        run_gates(keep[1.0], label="1x / 50% risk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
