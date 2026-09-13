"""
research/replay_btc.py — THE PHASE 1 ACCEPTANCE GATE.

Runs markets/engine.py and backtest_nostop.py over the SAME 6.9 years of
real BTC candles and asserts they produce the same trades.

WHY COMPARE ENGINES, NOT DOC NUMBERS
------------------------------------
The first version of this script checked against the README's published
"126 trades / PF 1.63 / $2,566" and reported a mismatch. The engine was
right and the target was stale: data_cache/ has grown since that figure was
written, so the reference backtest run on TODAY's cache produces 127 trades
and $2,531. Hard-coding a rounded number from a document turns every data
refresh into a false alarm, and a gate that cries wolf is a gate people
learn to skip.

So the gate is differential: same input, both engines, trade-by-trade
equality. It stays valid as the cache grows, and it tests the thing that
actually matters — that the universe-aware refactor did not change the
strategy on the one market where the right answer is known.

Deliberately OFFLINE. Candles and funding come from data_cache/, never the
network, so the result is reproducible rather than drifting with Binance's
amended history.

Run: ./venv/bin/python research/replay_btc.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "backtest"))

import pandas as pd                                              # noqa: E402

from corpus_manager import CorpusManager                          # noqa: E402
from markets.adapters.crypto_binance import OfflineCryptoAdapter  # noqa: E402
from markets.config import MarketConfig                           # noqa: E402
from markets.engine import MarketEngine                           # noqa: E402
from markets.state import new_state                               # noqa: E402
from markets.strategy import compute_indicators                   # noqa: E402

SYMBOL = "BTC/USDT:USDT"
INITIAL_BALANCE = 100.0
CORPUS_REFRESH = 10          # ratchet up after 10 net-positive, down after 10 losses
PNL_TOL = 0.005              # half a cent


def build_config() -> MarketConfig:
    """The locked production config, as a value.

    These are the validated output of ~30 backtests. Changing any of them
    invalidates every result the repo cites.
    """
    return MarketConfig(
        market_id="btc", symbols=(SYMBOL,), timeframe="15m", candle_minutes=15,
        leverage=5.0, risk_per_trade=0.10, max_margin_frac=1.0,
        maint_margin_rate=0.004, fee_rate=0.0005,
        rsi_len=14, macd_fast=12, macd_slow=26, macd_signal_win=9,
        vol_mult=2.0, vol_sma_period=20, atr_period=14,
        div_window=5, div_shift=5, div_memory=3,
        cb_trigger=5, cb_hours=48,
        dca_day=10, dca_monthly=10.0, dca_annual_growth=0.10, start_year=2019,
        gaps_overnight=False, max_concurrent_positions=1,
    )


def run_new_engine(df: pd.DataFrame, adapter, cfg: MarketConfig):
    cm = CorpusManager(
        initial_balance=INITIAL_BALANCE, base_monthly_dca=cfg.dca_monthly,
        dca_annual_growth=cfg.dca_annual_growth,
        ratchet_up_every=CORPUS_REFRESH, ratchet_down_after=CORPUS_REFRESH,
    )
    st = new_state(INITIAL_BALANCE)
    trades: list[dict] = []
    eng = MarketEngine(cfg, adapter, cm, st, exit_model="nostop", paper=True,
                       on_trade=trades.append)
    for n, (ts, row) in enumerate(df.iterrows()):
        # `now` is CANDLE time, never wall-clock. A 48h circuit-breaker pause
        # measured against today's clock would never expire while replaying
        # 2021, silently halting the run after the first five-loss streak.
        eng.process_candle(SYMBOL, row, n, ts.to_pydatetime())
        eng.end_of_bar()
    return trades, st


def main() -> int:
    cfg = build_config()
    ad = OfflineCryptoAdapter(symbol=SYMBOL, timeframe="15m")
    ad.connect()

    try:
        import backtest_leverage as bl
        import backtest_nostop as bn
    except ImportError:
        print("SKIP — backtest/ is gitignored research and is not present.\n"
              "       The gate needs it as the reference engine.")
        return 0

    # Warmup is trimmed BEFORE indicators, matching backtest_nostop.py:399-402.
    # Trimming after would leave a different set of NaN rows and therefore a
    # different first tradable bar.
    warmup = max(bl.RSI_LEN, bl.MACD_SLOW, bl.VOL_SMA_PERIOD, bl.ATR_PERIOD,
                 bl.DIV_WINDOW + bl.DIV_SHIFT) + 5
    raw = ad.all_candles().iloc[warmup:].copy()

    fpath = os.path.join(_ROOT, "data_cache",
                         "binanceusdm_BTC-USDT-USDT_funding.csv")
    f = pd.read_csv(fpath)
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True, format="mixed")
    funding = f.set_index("timestamp")["rate"].sort_index()

    years = (raw.index[-1] - raw.index[0]).days / 365.25
    print(f"  candles {len(raw):,}  [{years:.1f} years]  warmup {warmup}")

    ref = bn.simulate(bl.build_indicators(raw.copy()), 5, funding)
    ref_trades = ref["trades"]
    mine, st = run_new_engine(compute_indicators(raw.copy(), cfg), ad, cfg)

    print(f"\n  reference (backtest_nostop.py) : {len(ref_trades)} trades")
    print(f"  markets/engine.py              : {len(mine)} trades")

    if len(ref_trades) != len(mine):
        print(f"\n  FAIL — trade COUNT differs. A different number of trades is a "
              f"different strategy, not rounding.")
        return 1

    bad = []
    for i, (r, m) in enumerate(zip(ref_trades, mine)):
        # Parse both rather than slicing strings: the reference stores a
        # pandas Timestamp ("2020-01-23 12:00:00+00:00") and the engine an
        # ISO string ("2020-01-23T12:00:00+00:00"). Identical instants,
        # different separators — comparing the text reports 127/127 false
        # mismatches, which is how this comparator first "failed".
        r_ts = pd.Timestamp(r["date"])
        m_ts = pd.Timestamp(m["datetime"])
        if r_ts != m_ts:
            bad.append(f"#{i} date {r_ts} vs {m_ts}")
        elif r["side"] != m["side"] or r["reason"] != m["reason"]:
            bad.append(f"#{i} {r['side']}/{r['reason']} vs {m['side']}/{m['reason']}")
        elif abs(r["pnl"] - m["pnl"]) > PNL_TOL:
            bad.append(f"#{i} pnl {r['pnl']:.4f} vs {m['pnl']:.4f}")

    wins = [t for t in mine if t["pnl"] > 0]
    gl = -sum(t["pnl"] for t in mine if t["pnl"] <= 0)
    pf = (sum(t["pnl"] for t in wins) / gl) if gl > 0 else float("inf")
    print(f"  WR {100*len(wins)/len(mine):.2f}%   PF {pf:.2f}   "
          f"fees ${st['total_fees']:,.2f} (ref ${ref['total_fees']:,.2f})   "
          f"liquidations {sum(1 for t in mine if t['reason'] == 'liquidated')}")

    if bad:
        print(f"\n  FAIL — {len(bad)} of {len(mine)} trades differ:")
        for b in bad[:10]:
            print(f"    {b}")
        return 1

    # The reference reports the LAST CLOSED TRADE's balance as final; the
    # engine reports live state, which still carries any position open when
    # the data ends. Reconcile explicitly rather than papering over it.
    ref_final = ref_trades[-1]["balance"]
    open_pos = st["positions"].get(SYMBOL)
    print(f"\n  balance  reference ${ref_final:,.2f}  engine ${st['balance']:,.2f}")
    if open_pos:
        print(f"  delta explained: a {open_pos['side']} opened "
              f"{open_pos['entry_time'][:16]} (same bar the last trade closed) "
              f"and is still open, entry fee ${open_pos['entry_fee']:.4f} plus "
              f"funding since.")

    print(f"\n  PASS — all {len(mine)} trades identical to the reference engine.")
    print("         The universe-aware refactor did not change the strategy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
