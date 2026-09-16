"""BTC price/RSI — read-only, spot market, via ccxt. Same exchange client
pattern as notifier.py's RSIScanner (bot.py never touches spot, only futures).

Crypto has no earnings, no ROCE, no dividend record — it can never pass the
value screen and must never be labelled as having done so (see the scoped
RSI exception in ValueInvesting.md §7). This adapter only ever feeds the
PRICE_ONLY tier.
"""

from __future__ import annotations

import ccxt
import pandas as pd

from screener.indicators import latest_rsi

SYMBOL = "BTC/USDT"
TIMEFRAME = "1d"


def fetch_btc_snapshot() -> dict | None:
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        raw = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=60)
    except Exception:
        return None
    if not raw:
        return None
    closes = pd.Series([c[4] for c in raw])
    rsi = latest_rsi(closes)
    if rsi is None:
        return None
    return {"symbol": "BTC", "price": closes.iloc[-1], "rsi": rsi}
