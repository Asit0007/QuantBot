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

# Binance has no market-cap field; USDT-quoted stablecoins would sit at RSI
# ~50 forever and are meaningless to flag as "oversold", so they're excluded
# alongside leveraged tokens (…UP/…DOWN/…BULL/…BEAR), which track a multiple
# of the underlying rather than the coin itself.
_STABLECOINS = {
    "USDT", "USDC", "TUSD", "DAI", "FDUSD", "USDP", "BUSD", "EUR", "GBP", "TRY",
    "USD1", "RLUSD", "EURI", "USDE", "BFUSD", "XUSD",
}
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

# Binance's tokenized real-world-asset products (stocks/ETFs, "{TICKER}B"),
# NOT cryptocurrencies — misclassifying one of these as "crypto, oversold"
# in the digest would be an actual factual error, not just noise, so this is
# an explicit list rather than an "ends in B" pattern (SHIB is real crypto
# and also ends in B). Enumerated from the live top-220-by-volume window,
# verified 2026-09-16 — re-check this list if Binance adds new tokenized
# stock/ETF products that show up in a future top-N run.
_TOKENIZED_STOCKS = {
    "GOOGLB", "TSLAB", "NVDAB", "AVGOB", "INTCB", "MSTRB", "SNDKB", "ORCLB",
    "COINB", "CRCLB", "SPCXB", "SKHYB", "BMNRB", "LITEB", "KORUB", "QQQB",
    "SOXLB", "SNXXB", "BNCB",
}


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


def fetch_top_n_snapshot(n: int, progress=None) -> list[dict]:
    """Top N Binance USDT spot pairs by 24h quote volume, each with latest
    price + RSI(14) on daily closes. Volume rank is Binance's own — same
    "reproducible, no index committee" reasoning as the NSE turnover cut in
    universe.py, and it needs no second data source for market-cap ranking.

    ccxt has no bulk klines call, so this is N sequential fetch_ohlcv calls
    behind ccxt's built-in rate limiter — budget a couple of minutes for
    n=200. Any symbol whose OHLCV fetch fails is skipped, not fatal to the
    rest (same fail-closed-per-item pattern as fetch_fundamentals).
    """
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        tickers = exchange.fetch_tickers()
    except Exception:
        return []

    candidates = []
    for market_symbol, ticker in tickers.items():
        if not market_symbol.endswith("/USDT"):
            continue
        base = market_symbol.split("/")[0]
        if base in _STABLECOINS or base in _TOKENIZED_STOCKS or base.endswith(_LEVERAGED_SUFFIXES):
            continue
        vol = ticker.get("quoteVolume")
        if vol:
            candidates.append((market_symbol, base, vol))
    candidates.sort(key=lambda c: c[2], reverse=True)

    out = []
    for i, (market_symbol, base, _) in enumerate(candidates[:n], 1):
        if progress:
            progress(i, min(n, len(candidates)), base)
        try:
            raw = exchange.fetch_ohlcv(market_symbol, TIMEFRAME, limit=60)
        except Exception:
            continue
        if not raw:
            continue
        closes = pd.Series([c[4] for c in raw])
        rsi = latest_rsi(closes)
        if rsi is None:
            continue
        out.append({"symbol": base, "price": closes.iloc[-1], "rsi": rsi})
    return out
