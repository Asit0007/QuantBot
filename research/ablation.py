"""
╔══════════════════════════════════════════════════════════════════════╗
║  research/ablation.py — does each signal gate earn its place?         ║
╠══════════════════════════════════════════════════════════════════════╣
║  ABLATION IS NOT A SWEEP, and the distinction is the whole point.     ║
║                                                                      ║
║  A sweep asks "which combination scores best?" — that is SELECTION.   ║
║  It inflates the trial count that gate 6 then punishes, and with      ║
║  enough attempts something always looks good.                         ║
║                                                                      ║
║  Ablation asks "is each component earning its place?" — that is       ║
║  FALSIFICATION. Three pre-specified tests, not five hundred, and the  ║
║  answer cannot be cherry-picked because each run is a fixed question  ║
║  about the EXISTING config rather than a search for a new one.        ║
║                                                                      ║
║  WHY THIS RUN EXISTS. backtest_sensitivity.py is cited as proving the ║
║  edge sits on a plateau ("18/18 neighbouring values profitable"). It  ║
║  swept DIV_WINDOW, DIV_SHIFT, DIV_MEMORY, VOL_MULT, RSI_LEN and       ║
║  VOL_SMA_PERIOD — and NOT MACD_FAST/SLOW/SIGNAL. MACD appears ZERO    ║
║  times in the 552-line research log while sitting in 34 of 44         ║
║  backtest scripts and in the live production entry. Its 12/26/9 are   ║
║  textbook defaults carried in unexamined. The plateau claim covers    ║
║  two of the three gates, not three.                                   ║
║                                                                      ║
║  METHOD. A gate is removed by forcing its column TRUE for every bar,  ║
║  which deletes it as a filter while leaving everything else — sizing, ║
║  stops, circuit breaker, funding, ratchet — untouched. Because entry  ║
║  and exit read the SAME columns, a gate is ablated symmetrically      ║
║  rather than only on the way in.                                      ║
║                                                                      ║
║  READING THE RESULT:                                                  ║
║   * removing a gate HURTS  -> it earns its place                      ║
║   * removing it does nothing -> it is decoration, and decoration is   ║
║     where overfitting hides: every unnecessary parameter is free rope ║
║   * removing it HELPS -> production has been carrying a harmful       ║
║     filter for its entire life                                        ║
╚══════════════════════════════════════════════════════════════════════╝

Usage: ./venv/bin/python research/ablation.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_manager import CorpusManager                           # noqa: E402
from markets.adapters.crypto_binance import OfflineCryptoAdapter   # noqa: E402
from markets.engine import MarketEngine                            # noqa: E402
from markets.state import new_state                                # noqa: E402
from markets.strategy import compute_indicators                    # noqa: E402
from replay_btc import SYMBOL, INITIAL_BALANCE, CORPUS_REFRESH, build_config  # noqa: E402
from robustness import run_gates                                   # noqa: E402

# Each ablation forces these columns TRUE, deleting that gate as a filter.
ABLATIONS = {
    "BASELINE (all 3 gates)": [],
    "no RSI-divergence": ["bull_div", "bear_div"],
    "no MACD cross": ["macd_bull_cross", "macd_bear_cross"],
    "no volume spike": ["high_vol"],
}


def run_one(df: pd.DataFrame, cfg, drop: list[str], adapter):
    d = df.copy()
    for col in drop:
        d[col] = True
    cm = CorpusManager(initial_balance=INITIAL_BALANCE,
                       base_monthly_dca=cfg.dca_monthly,
                       dca_annual_growth=cfg.dca_annual_growth,
                       ratchet_up_every=CORPUS_REFRESH,
                       ratchet_down_after=CORPUS_REFRESH)
    st = new_state(INITIAL_BALANCE)
    trades: list[dict] = []
    eng = MarketEngine(cfg, adapter, cm, st, exit_model="nostop", paper=True,
                       on_trade=trades.append)
    for n, (ts, row) in enumerate(d.iterrows()):
        eng.process_candle(SYMBOL, row, n, ts.to_pydatetime())
        eng.end_of_bar()
    return trades, st


def main() -> int:
    cfg = build_config()
    ad = OfflineCryptoAdapter(symbol=SYMBOL, timeframe="15m")
    ad.connect()
    warmup = 31
    df = compute_indicators(ad.all_candles().iloc[warmup:].copy(), cfg)
    print(f"BTC gate ablation — {len(df):,} candles, "
          f"{df.index[0].date()} -> {df.index[-1].date()}\n")

    print(f"  {'variant':<24}{'trades':>8}{'WR':>8}{'PF':>8}{'final $':>12}{'liq':>6}{'fees':>9}",
          flush=True)
    print("  " + "-" * 75, flush=True)
    results = {}
    for name, drop in ABLATIONS.items():
        tr, st = run_one(df, cfg, drop, ad)
        results[name] = tr
        if not tr:
            print(f"  {name:<24}{'0':>8}{'-':>8}{'-':>8}{st['balance']:>12,.0f}", flush=True)
            continue
        p = [t["pnl"] for t in tr]
        w = [x for x in p if x > 0]
        gl = -sum(x for x in p if x <= 0)
        pf = (sum(w) / gl) if gl > 0 else float("inf")
        liq = sum(1 for t in tr if t["reason"] == "liquidated")
        print(f"  {name:<24}{len(tr):>8}{100*len(w)/len(tr):>7.1f}%{pf:>8.2f}"
              f"{st['balance']:>12,.0f}{liq:>6}{st['total_fees']:>9,.0f}", flush=True)

    # Gate scoring is deliberately SEPARATE and opt-in. Leave-one-year-out
    # runs 400 controls x 1,500 bootstraps per year, which is fine for a
    # 127-trade baseline and pathological for an ablation that produces
    # thousands. Run it with --gates when the table says a variant is worth
    # the wait.
    if "--gates" in sys.argv:
        for name, tr in results.items():
            if 10 <= len(tr) <= 2000:
                run_gates(tr, label=name, verbose=True)
            elif len(tr) > 2000:
                print(f"\n  {name}: {len(tr)} trades — skipping LOYO "
                      f"(cost is quadratic-ish in trade count)")
    return 0




# ══════════════════════════════════════════════════════════════════════
#  MACD SENSITIVITY — closing the gap the ablation exposed
# ══════════════════════════════════════════════════════════════════════
# backtest_sensitivity.py sweeps DIV_WINDOW, DIV_SHIFT, DIV_MEMORY,
# VOL_MULT, RSI_LEN and VOL_SMA_PERIOD, and its "18/18 neighbours
# profitable" result is cited as proof the edge sits on a plateau rather
# than a fitted spike. MACD_FAST/SLOW/SIGNAL are NOT in that sweep, so the
# plateau claim covered two of the three gates.
#
# This is a SENSITIVITY test, not a search: each parameter is moved one
# step either way from production and the question is "does the edge
# survive?", never "which value is best". Picking a winner here would be
# exactly the selection this whole file argues against.

MACD_NEIGHBOURS = [
    ("macd_fast", 12, [10, 12, 14]),
    ("macd_slow", 26, [22, 26, 30]),
    ("macd_signal_win", 9, [7, 9, 11]),
]


def macd_sensitivity() -> int:
    from dataclasses import replace
    base = build_config()
    ad = OfflineCryptoAdapter(symbol=SYMBOL, timeframe="15m")
    ad.connect()
    raw = ad.all_candles().iloc[31:].copy()

    print("\nMACD SENSITIVITY — one step either side of production\n")
    print(f"  {'param':<18}{'value':>7}{'trades':>8}{'WR':>8}{'PF':>8}{'final $':>11}")
    print("  " + "-" * 60)
    ok = tot = 0
    for name, prod, vals in MACD_NEIGHBOURS:
        for v in vals:
            cfg = replace(base, **{name: v})
            df = compute_indicators(raw, cfg)
            tr, st = run_one(df, cfg, [], ad)
            if not tr:
                print(f"  {name:<18}{v:>7}{'0':>8}")
                continue
            p = [t["pnl"] for t in tr]
            w = [x for x in p if x > 0]
            gl = -sum(x for x in p if x <= 0)
            pf = (sum(w) / gl) if gl > 0 else float("inf")
            tag = "  <- production" if v == prod else ""
            tot += 1
            ok += int(pf > 1.0)
            print(f"  {name:<18}{v:>7}{len(tr):>8}{100*len(w)/len(tr):>7.1f}%"
                  f"{pf:>8.2f}{st['balance']:>11,.0f}{tag}", flush=True)
    print(f"\n  {ok}/{tot} MACD neighbours have PF > 1.")
    print("  A plateau means the edge is not an artefact of the exact values;")
    print("  a spike would mean 12/26/9 were fitted.")
    return 0


if __name__ == "__main__":
    if "--macd" in sys.argv:
        sys.exit(macd_sensitivity())
    sys.exit(main())
