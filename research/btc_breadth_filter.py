"""
research/btc_breadth_filter.py — global breadth as a regime filter on the
PRODUCTION BTC bot.

The question: the bot is validated (PF 1.60, 127 trades, 13 liquidations).
Global breadth independently predicts BTC out-of-sample. Does gating the bot's
ENTRIES on breadth improve it?

Entries only — never exits. Blocking an exit would leave a position open that
the strategy wanted closed, which is a risk control, not a filter.

NO LOOKAHEAD. Breadth is computed on a TRAILING window (the centred version
used earlier in this session marked a bar as "signal fired recently" up to
three days BEFORE the signal existed), and the daily series is then SHIFTED
ONE DAY before being mapped onto 15m bars — breadth from day D's close is only
available to bars on day D+1.

Variants are pre-specified, not swept: no filter, b>=2, b>=3, b==4.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_manager import CorpusManager                            # noqa: E402
from cross_market import AGREE_WINDOW, macd_cross                   # noqa: E402
from markets.adapters.crypto_binance import OfflineCryptoAdapter    # noqa: E402
from markets.adapters.us_french import load_region                  # noqa: E402
from markets.engine import MarketEngine                             # noqa: E402
from markets.state import new_state                                 # noqa: E402
from markets.strategy import compute_indicators                     # noqa: E402
from replay_btc import (CORPUS_REFRESH, INITIAL_BALANCE,            # noqa: E402
                        SYMBOL, build_config)
from robustness import profit_factor                                # noqa: E402

REGIONS = ["EUROPE", "JAPAN", "ASIAPAC", "NORTHAM"]


def breadth_daily() -> pd.Series:
    px = pd.DataFrame({r: load_region(r)["close"] for r in REGIONS}).dropna()
    sig = pd.DataFrame({r: macd_cross(px[r]) for r in REGIONS})
    near = sig.rolling(2 * AGREE_WINDOW + 1, min_periods=1).max().astype(bool)
    b = near.sum(axis=1)
    # Shift one day: breadth from day D's close is only knowable on D+1.
    b = b.shift(1)
    b.index = pd.to_datetime(b.index).normalize()
    return b.dropna()


def run(df, cfg, ad, min_breadth):
    cm = CorpusManager(initial_balance=INITIAL_BALANCE,
                       base_monthly_dca=cfg.dca_monthly,
                       dca_annual_growth=cfg.dca_annual_growth,
                       ratchet_up_every=CORPUS_REFRESH,
                       ratchet_down_after=CORPUS_REFRESH)
    st = new_state(INITIAL_BALANCE)
    trades = []
    eng = MarketEngine(cfg, ad, cm, st, exit_model="nostop", paper=True,
                       on_trade=trades.append)
    for n, (ts, row) in enumerate(df.iterrows()):
        if min_breadth is not None and row["breadth"] < min_breadth:
            # Suppress ENTRIES only: run the candle with the entry gates off.
            r = row.copy()
            r["bull_div"] = False
            r["bear_div"] = False
            eng.process_candle(SYMBOL, r, n, ts.to_pydatetime())
        else:
            eng.process_candle(SYMBOL, row, n, ts.to_pydatetime())
        eng.end_of_bar()
    return trades, st


def main() -> int:
    cfg = build_config()
    ad = OfflineCryptoAdapter(symbol=SYMBOL, timeframe="15m")
    ad.connect()
    df = compute_indicators(ad.all_candles().iloc[31:].copy(), cfg)

    b = breadth_daily()
    day = pd.Series(df.index.tz_localize(None).normalize(), index=df.index)
    df["breadth"] = day.map(b).ffill()
    cov = df["breadth"].notna().mean()
    df = df[df["breadth"].notna()]
    print(f"BTC bot + global breadth filter (entries only, no lookahead)")
    print(f"  {len(df):,} candles with breadth coverage ({cov:.0%} of the sample)")
    print(f"  breadth available {df.index[0].date()} -> {df.index[-1].date()}\n")

    print(f"  {'variant':<22}{'trades':>8}{'WR':>8}{'PF':>7}{'final $':>10}"
          f"{'liq':>6}{'fees':>8}{'exposure':>10}")
    print("  " + "-" * 80)
    for label, mb in [("baseline (no filter)", None), ("breadth >= 2", 2),
                      ("breadth >= 3", 3), ("breadth == 4", 4)]:
        tr, st = run(df, cfg, ad, mb)
        if not tr:
            print(f"  {label:<22}{'0':>8}")
            continue
        p = np.array([t["pnl"] for t in tr])
        w = p[p > 0]
        liq = sum(1 for t in tr if t["reason"] == "liquidated")
        held = sum(t["hold_candles"] for t in tr)
        print(f"  {label:<22}{len(tr):>8}{100*len(w)/len(p):>7.1f}%"
              f"{profit_factor(p):>7.2f}{st['balance']:>10,.0f}{liq:>6}"
              f"{st['total_fees']:>8,.0f}{held/len(df):>9.0%}")
    print("\n  A filter that removes trades must improve PF or cut liquidations")
    print("  by more than it costs in forgone profit. Fewer trades is not a win.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
