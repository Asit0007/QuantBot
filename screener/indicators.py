"""RSI, computed the same way bot.py's live signal does (ta.momentum.RSIIndicator,
period 14) so a reader familiar with the BTC bot's RSI radar reads this the same way.
"""

from __future__ import annotations

import pandas as pd
from ta.momentum import RSIIndicator

RSI_PERIOD = 14


def latest_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float | None:
    """Latest RSI(period) value from a close-price series (oldest -> newest).

    None if there isn't enough history — RSI needs `period` deltas to settle,
    a handful more to stop drifting from its initial seed.
    """
    closes = closes.dropna()
    if len(closes) < period + 5:
        return None
    rsi = RSIIndicator(close=closes, window=period).rsi()
    value = rsi.iloc[-1]
    return None if pd.isna(value) else float(value)
