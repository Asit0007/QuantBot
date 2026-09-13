"""
markets/registry.py — the one place that knows which markets exist.

Follows the pattern already proven in this codebase at
JobPipe/src/jobpipe/sources/__init__.py: a dict of id -> factory, so adding
a market is one entry rather than an edit scattered across the engine.

Factories, not instances. Building a market touches the network (venue
metadata) and imports optional dependencies (exchange_calendars), and doing
that at import time would make a box that only runs BTC fail on an NSE
import it never uses.
"""
from __future__ import annotations

from .config import MarketConfig

# BTC — the incumbent. A universe of one, which is what lets the same
# universe-based engine serve it and a 50-symbol Nifty basket unchanged.
# gaps_overnight=False is the fact that makes nostop legal here and only
# here (markets/strategy.py:require_exit_model_safe).
BTC = MarketConfig(
    market_id="btc",
    symbols=("BTC/USDT:USDT",),
    timeframe="15m",
    candle_minutes=15,
    leverage=5.0,
    risk_per_trade=0.10,
    gaps_overnight=False,
    max_concurrent_positions=1,
    benchmarks={"wr": 0.540, "pf": 1.63},
)

# NSE — cross-sectional over Nifty 50 constituents.
#
# symbols is EMPTY on purpose. Populating it with today's index membership
# and applying that backwards over history is survivorship bias, and it is
# the single most common way to manufacture a fake edge in equity research:
# the companies that were dropped from the index were dropped for doing
# badly, so a universe of current members has quietly excluded the losers.
# Phase 2 must source point-in-time constituents before this is filled in.
#
# timeframe/candle_minutes are placeholders pending the Phase 0 data spike:
# free intraday Indian history is thin, and if only daily bars are
# available at depth then daily is the honest answer — the BTC edge is
# already described as "a swing strategy that happens to trigger on 15m
# candles" at ~18 trades/yr and ~18-day holds, so daily bars match the edge
# shape rather than compromising it.
NSE_STOCKS = MarketConfig(
    market_id="nse_stocks",
    symbols=(),
    timeframe="1d",
    candle_minutes=1440,
    leverage=1.0,
    risk_per_trade=0.02,
    gaps_overnight=True,
    max_concurrent_positions=5,
    benchmarks={},
)

# US indices — SPX / NDX / DJI.
#
# These are ~0.9 correlated. That is acceptable for what they were chosen
# for (trade samples, portfolio story) but they are NOT three independent
# risk exposures, and backtest_symbols.py already recorded what happens when
# correlated symbols share an account: PF 1.03 at 82.6% DD, because the
# signal fires on all of them at the same moments and the portfolio takes
# the same bet 4-5x instead of diversifying.
#
# Hence ONE shared corpus plus max_concurrent_positions. That combination is
# precisely the experiment backtest/README.md says was never run: "the shape
# to test is 4 symbols at LOWER leverage with a cap on concurrent positions."
US_INDICES = MarketConfig(
    market_id="us_indices",
    symbols=("SPY", "QQQ", "DIA"),
    timeframe="1d",
    candle_minutes=1440,
    leverage=1.0,
    risk_per_trade=0.02,
    gaps_overnight=True,
    max_concurrent_positions=1,
    benchmarks={},
)

ALL_MARKETS = {
    "btc": BTC,
    "nse_stocks": NSE_STOCKS,
    "us_indices": US_INDICES,
}


def get_market(market_id: str) -> MarketConfig:
    if market_id not in ALL_MARKETS:
        raise KeyError(f"Unknown market '{market_id}'. Known: {sorted(ALL_MARKETS)}")
    return ALL_MARKETS[market_id]
