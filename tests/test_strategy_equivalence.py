"""
Proves markets/strategy.py is arithmetically identical to bot.py, on the
real 6.9 years of BTC candles the live config was derived from.

WHY
---
markets/strategy.py reimplements bot.py's pure functions with config passed
as a value instead of read from module globals. "Reimplements" is the risky
word: bot.py is the reference implementation behind a validated backtest
(126 trades, PF 1.63, $2,566), and the go-live gate is a paper-vs-backtest
comparison that means nothing if the two engines quietly diverged.

So this does not test markets/strategy.py against my expectations. It tests
it against bot.py, on real data, column by column, exact equality including
NaN placement.

Run: ./venv/bin/python tests/test_strategy_equivalence.py
"""
import os
import sys
import tempfile

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="quantbot-test-")

import bot                                          # noqa: E402
from markets.config import MarketConfig             # noqa: E402
import markets.strategy as strat                    # noqa: E402

CANDLES = os.path.join(_ROOT, "data_cache", "binanceusdm_BTC-USDT-USDT_15m.csv")


def load_candles() -> pd.DataFrame:
    df = pd.read_csv(CANDLES)
    tcol = next(c for c in df.columns
                if c.lower() in ("timestamp", "datetime", "time", "date"))
    df[tcol] = pd.to_datetime(df[tcol], utc=True, format="mixed")
    return df.set_index(tcol).sort_index()


def main() -> int:
    if not os.path.exists(CANDLES):
        print(f"SKIP — no cached candles at {CANDLES}")
        return 0

    df = load_candles()
    # Built from the SAME .env bot.py just loaded, so any difference in
    # output is a difference in ARITHMETIC, not in configuration.
    cfg = MarketConfig.from_env("btc")

    print(f"candles      : {len(df):,}  ({df.index[0].date()} -> {df.index[-1].date()})")
    print(f"config       : lev {cfg.leverage}x, risk {cfg.risk_per_trade:.0%}, "
          f"RSI {cfg.rsi_len}, MACD {cfg.macd_fast}/{cfg.macd_slow}/{cfg.macd_signal_win}")

    ref = bot.compute_indicators(df)
    new = strat.compute_indicators(df, cfg)

    failures = []

    if list(ref.columns) != list(new.columns):
        failures.append(f"column sets differ: {set(ref.columns) ^ set(new.columns)}")
    else:
        for col in ref.columns:
            a, b = ref[col], new[col]
            if a.dtype == bool or b.dtype == bool:
                if not a.equals(b):
                    failures.append(f"{col}: {(a != b).sum():,} rows differ")
                continue
            # Exact equality, NaN-for-NaN. Not np.isclose — a reimplementation
            # that is merely CLOSE has changed the arithmetic, and over 126
            # trades and 6.9 years small drifts compound into different trades.
            if not np.array_equal(a.to_numpy(), b.to_numpy(), equal_nan=True):
                d = (~np.isclose(a.to_numpy(), b.to_numpy(),
                                 rtol=0, atol=0, equal_nan=True)).sum()
                failures.append(f"{col}: {d:,} rows differ (max abs diff "
                                f"{np.nanmax(np.abs(a.to_numpy() - b.to_numpy())):.3e})")

    print(f"indicators   : {len(ref.columns)} columns compared over {len(ref):,} rows")

    # ── sizing sweep ─────────────────────────────────────────────────
    checked = 0
    for corpus in (100.0, 250.0, 1000.0, 2569.0):
        for price in (8285.0, 30000.0, 79800.0, 120000.0):
            r = bot.size_position_nostop(corpus, price)
            n = strat.size_position_nostop(corpus, price, cfg)
            for k in ("margin", "notional", "qty", "stop_distance", "dollar_risk"):
                checked += 1
                if r[k] != n[k]:
                    failures.append(f"size_nostop[{k}] corpus={corpus} price={price}: "
                                    f"{r[k]!r} vs {n[k]!r}")
            for free in (10.0, 50.0, corpus):
                rf = bot.fit_to_margin(dict(r), free)
                nf = strat.fit_to_margin(dict(n), free, cfg)
                for k in rf:
                    checked += 1
                    if rf[k] != nf[k]:
                        failures.append(f"fit_to_margin[{k}] free={free}: "
                                        f"{rf[k]!r} vs {nf[k]!r}")
            for side in ("long", "short"):
                checked += 1
                if bot.liq_price(side, price) != strat.liq_price(side, price, cfg):
                    failures.append(f"liq_price[{side}] price={price}")

    print(f"sizing       : {checked:,} scalar comparisons")

    if failures:
        print("\nFAIL — markets/strategy.py diverges from bot.py:")
        for f in failures[:10]:
            print(f"  {f}")
        return 1

    print("\nPASS — markets/strategy.py is arithmetically identical to bot.py")
    print("       across 6.9 years of real candles. Config-as-value changed")
    print("       nothing but where the numbers come from.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
