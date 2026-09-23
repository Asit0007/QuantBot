"""
╔══════════════════════════════════════════════════════════════════════╗
║  research/nse_cross_sectional.py — PHASE 2 baseline                  ║
╠══════════════════════════════════════════════════════════════════════╣
║  Runs the engine across a point-in-time NSE equity universe on daily  ║
║  bars, then scores the result with the five robustness gates.         ║
║                                                                      ║
║  THIS IS A BASELINE, NOT A STRATEGY. It deliberately starts from the  ║
║  BTC signal shape (RSI divergence + MACD cross + volume spike) as a   ║
║  HYPOTHESIS to be tested, not a design to be trusted. The BTC config  ║
║  was validated on one asset that trades 24/7 with no gaps, no         ║
║  short-selling restriction and no dividend. None of that holds here.  ║
║  A pass would be interesting; a fail is the expected outcome and is   ║
║  a perfectly good result to report.                                   ║
║                                                                      ║
║  FOUR THINGS DONE DIFFERENTLY FROM BTC, EACH FOR A REASON:            ║
║                                                                      ║
║  1. REAL STOPS. nostop is refused outright for this market            ║
║     (markets/strategy.py). Its loss ceiling assumes price moves       ║
║     continuously to the liquidation level; NSE is shut 18h a day and  ║
║     gaps straight through it.                                         ║
║  2. LONG ONLY. In the Indian CASH segment a retail short must be      ║
║     squared off the same day — holding one overnight needs F&O or     ║
║     stock lending. A daily backtest that shorts and holds is          ║
║     untradeable, and would look like free alpha because roughly half  ║
║     the signals are shorts.                                           ║
║  3. NO LEVERAGE. 1x. Cash equities.                                   ║
║  4. POINT-IN-TIME UNIVERSE, rebalanced monthly on TRAILING turnover.  ║
║     Using today's Nifty 50 across history is survivorship bias — the  ║
║     single most common way to manufacture a fake edge here.           ║
║                                                                      ║
║  COUNT EVERY CONFIGURATION YOU TEST. QuantBot already has ~100 tested ║
║  against one BTC sample, which is why its own research log concludes  ║
║  test #101 is less trustworthy than test #1. Log each run below.      ║
╚══════════════════════════════════════════════════════════════════════╝

Usage:
    ./venv/bin/python research/nse_cross_sectional.py [start] [end]
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_manager import CorpusManager                            # noqa: E402
from markets.adapters.nse_bhavcopy import (BhavcopyStore,           # noqa: E402
                                           NSEBhavcopyAdapter, build_universe)
from markets.config import MarketConfig                             # noqa: E402
from markets.engine import MarketEngine                             # noqa: E402
from markets.state import new_state                                 # noqa: E402
from markets.strategy import compute_indicators                     # noqa: E402
from overfitting import run_extended                                # noqa: E402
from robustness import run_gates                                    # noqa: E402

START_BALANCE = 100_000.0        # INR. A realistic retail account, not $100.
UNIVERSE_SIZE = 50
REBALANCE_DAYS = 21              # ~monthly
WARMUP_BARS = 60                 # daily bars needed before indicators are valid

# Every configuration ever run against NSE data. Increment it when you add
# one — including the ones you discard. Under-reporting this is the single
# easiest way to make gate 6 lie to you, and it is the input nobody wants to
# fill in truthfully.
NSE_TRIALS_SO_FAR = 3            # config 1 (partial), 1b (full), 1c (adjusted)


def nse_config(**overrides) -> MarketConfig:
    base = dict(
        market_id="nse_stocks", symbols=(), timeframe="1d", candle_minutes=1440,
        leverage=1.0,               # cash equities
        risk_per_trade=0.02,        # 2% of corpus at the stop, not BTC's 10%
        max_margin_frac=1.0, fee_rate=0.001,   # ~0.1%: brokerage+STT+stamp+exchange
        long_atr_mult=3.0, short_atr_mult=3.0,
        rsi_len=14, macd_fast=12, macd_slow=26, macd_signal_win=9,
        vol_mult=2.0, vol_sma_period=20, atr_period=14,
        div_window=5, div_shift=5, div_memory=3,
        cb_trigger=5, cb_hours=48,
        dca_day=10, dca_monthly=0.0, dca_annual_growth=0.0, start_year=2015,
        gaps_overnight=True,        # forces real stops; refuses nostop
        allow_short=False,          # NSE cash: no overnight shorts
        max_concurrent_positions=5,
    )
    base.update(overrides)
    return MarketConfig(**base)


def load_panel(start: str, end: str) -> pd.DataFrame:
    store = BhavcopyStore()
    cached = sorted(f[:-7] for f in os.listdir(store.cache_dir)
                    if f.endswith(".csv.gz"))
    days = [d for d in cached if start <= d <= end]
    if not days:
        raise SystemExit(f"no cached sessions in {start}..{end}")
    frames = [pd.read_csv(os.path.join(store.cache_dir, f"{d}.csv.gz"),
                          parse_dates=["date"]) for d in days]
    return pd.concat(frames, ignore_index=True)


def run_study(panel: pd.DataFrame, cfg: MarketConfig, *,
              universe_size: int = UNIVERSE_SIZE,
              rebalance_days: int = REBALANCE_DAYS, verbose: bool = True,
              indicator_fn=None, exit_model: str = "stop"):
    dates = sorted(panel["date"].unique())
    if len(dates) < WARMUP_BARS * 2:
        raise SystemExit(f"only {len(dates)} sessions — need more history")

    # Rebalance schedule, and the union of everything ever selected. Only
    # that union needs indicators, which turns ~2,900 symbols into ~200.
    reb_idx = list(range(WARMUP_BARS, len(dates), rebalance_days))
    universes: dict[pd.Timestamp, list[str]] = {}
    for i in reb_idx:
        universes[dates[i]] = build_universe(panel, dates[i], top_n=universe_size)
    union = sorted({s for u in universes.values() for s in u})
    if verbose:
        print(f"  sessions {len(dates)}  rebalances {len(universes)}  "
              f"symbols ever selected {len(union)}")

    ad = NSEBhavcopyAdapter(panel=panel)
    frames: dict[str, pd.DataFrame] = {}
    for s in union:
        f = ad.symbol_frame(s)
        if len(f) >= WARMUP_BARS + 20:
            # compute_indicators always runs: it supplies ATR, which the
            # protective stop needs regardless of which signal is layered on.
            g = compute_indicators(f, cfg)
            frames[s] = indicator_fn(g, cfg) if indicator_fn else g
    if verbose:
        print(f"  symbols with enough history: {len(frames)}")

    cm = CorpusManager(initial_balance=START_BALANCE, base_monthly_dca=0.0,
                       dca_annual_growth=0.0, ratchet_up_every=10,
                       ratchet_down_after=10)
    st = new_state(START_BALANCE)
    trades: list[dict] = []
    eng = MarketEngine(cfg, ad, cm, st, exit_model=exit_model, paper=True,
                       on_trade=trades.append)

    current: list[str] = []
    t0 = time.time()
    for n, d in enumerate(dates):
        if d in universes:
            current = universes[d]
        if n < WARMUP_BARS:
            continue

        # Symbols to process = the universe, PLUS anything already held.
        # A position is not force-closed just because its symbol dropped out
        # of a liquidity ranking — that would be an artefact of the universe
        # rule, not a trading decision.
        held = list(st["positions"].keys())
        # Deterministic, PRE-COMMITTED order. When more signals fire than
        # max_concurrent_positions allows, whoever is processed first wins,
        # so this ordering IS the ranking rule. Alphabetical is the honest
        # null: ranking on anything computed from the bar being traded would
        # be a lookahead back door.
        todo = sorted(set(current) | set(held))

        for sym in todo:
            f = frames.get(sym)
            if f is None:
                continue
            try:
                row = f.loc[d]
            except KeyError:
                continue                      # not traded that day
            eng.process_candle(sym, row, n, pd.Timestamp(d).to_pydatetime())
        eng.end_of_bar()

        if verbose and n % 500 == 0:
            print(f"    {d.date()}  trades {len(trades)}  "
                  f"balance {st['balance']:,.0f}", flush=True)

    if verbose:
        print(f"  ran in {time.time()-t0:.0f}s")
    return trades, st


def summarise(trades, st, label):
    if not trades:
        print(f"\n  {label}: NO TRADES")
        return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    print(f"\n  {label}")
    print(f"    trades {len(trades)}   WR {100*len(wins)/len(trades):.1f}%   "
          f"net {sum(pnls):+,.0f}   final balance {st['balance']:,.0f} "
          f"({st['balance']/START_BALANCE - 1:+.1%})")
    print(f"    fees {st['total_fees']:,.0f}   "
          f"symbols traded {len({t['symbol'] for t in trades})}")


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 else "2015-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-09-12"
    print(f"NSE cross-sectional baseline  {start} -> {end}")
    print("CONFIG #1 of this study — long only, ATR stop, 1x, top-50 by turnover\n")

    panel = load_panel(start, end)
    print(f"  panel {len(panel):,} rows, {panel['symbol'].nunique():,} symbols, "
          f"{panel['date'].nunique():,} sessions")

    cfg = nse_config()
    trades, st = run_study(panel, cfg)
    summarise(trades, st, "BASELINE (RSI div + MACD + volume, long only)")
    run_gates(trades, label="BASELINE")
    # n_trials is the honest count of configurations tried against NSE data.
    # The ~100 BTC trials do not count here — different data — but every NSE
    # variant from here on must increment it.
    run_extended(trades, n_trials=NSE_TRIALS_SO_FAR, label="BASELINE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
