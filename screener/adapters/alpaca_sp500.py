"""
US equities (S&P 500) — price + RSI via Alpaca's free market-data API.

PRICE ONLY, same tier as ETFs/REITs/InvITs/SGBs/crypto: Alpaca has no
fundamentals endpoint, so there is no Graham value screen for this tier —
building one would need a separate verified fundamentals source, not
attempted here (see ValueInvesting.md's scoped RSI exception: RSI-only is
fine precisely because nothing here claims "value" qualification).

Constituent list: GitHub's maintained `datasets/s-and-p-500-companies` CSV
(free, no key, verified 2026-09-17), cached locally with a 30-day TTL — S&P
500 membership changes only a handful of times a year, unlike NSE turnover
which is recomputed every run.

Ranking: same "reproducible, no index committee" turnover approach as NSE
and crypto — average dollar volume (close x volume) over the fetch window,
not a market-cap figure (Alpaca's free tier has none).

Alpaca's multi-symbol bars endpoint paginates once the combined bar count
crosses ~1500-2000 rows — verified live 2026-09-17: 503 symbols x ~69 daily
bars needs 4 pages, ~16s total. One paginated fetch gets both the turnover
ranking AND the price history for RSI, so unlike Binance (no bulk klines
call) there is no second per-symbol fetch loop.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path

import pandas as pd
import requests

from screener import config
from screener.indicators import latest_rsi

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "sp500"
CONSTITUENTS_CACHE = CACHE_DIR / "constituents.csv"
CONSTITUENTS_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)
CONSTITUENTS_TTL_DAYS = 30

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
LOOKBACK_DAYS = 100  # calendar days -> ~69 trading days, enough for RSI(14)


def fetch_constituents(force_refresh: bool = False) -> list[str]:
    """S&P 500 ticker list, cached locally with a 30-day TTL."""
    if not force_refresh and CONSTITUENTS_CACHE.exists():
        age_days = (
            dt.datetime.now() - dt.datetime.fromtimestamp(CONSTITUENTS_CACHE.stat().st_mtime)
        ).days
        if age_days < CONSTITUENTS_TTL_DAYS:
            with open(CONSTITUENTS_CACHE) as f:
                return [row["Symbol"] for row in csv.DictReader(f)]

    resp = requests.get(CONSTITUENTS_URL, timeout=30)
    resp.raise_for_status()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONSTITUENTS_CACHE.write_text(resp.text)
    return [row["Symbol"] for row in csv.DictReader(io.StringIO(resp.text))]


def _headers() -> dict | None:
    if not config.ALPACA_API_KEY_ID or not config.ALPACA_API_SECRET_KEY:
        return None
    return {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_API_SECRET_KEY,
    }


def fetch_top_n_snapshot(n: int, progress=None) -> list[dict]:
    """Top N S&P 500 names by average dollar volume, each with latest price
    + RSI(14). Returns [] if Alpaca isn't configured or unreachable — fails
    closed, same as the Binance leg when it can't be reached.
    """
    headers = _headers()
    if headers is None:
        return []

    try:
        symbols = fetch_constituents()
    except Exception:
        return []

    start = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    all_bars: dict[str, list] = {}
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 10000}
    while True:
        try:
            resp = requests.get(BARS_URL, headers=headers, params=params, timeout=60)
            resp.raise_for_status()
        except Exception:
            break
        data = resp.json()
        all_bars.update(data.get("bars", {}))
        token = data.get("next_page_token")
        if not token:
            break
        params["page_token"] = token

    ranked = []
    for symbol, bars in all_bars.items():
        if not bars:
            continue
        avg_dollar_vol = sum(b["c"] * b["v"] for b in bars) / len(bars)
        ranked.append((symbol, bars, avg_dollar_vol))
    ranked.sort(key=lambda r: r[2], reverse=True)

    out = []
    top = ranked[:n]
    for i, (symbol, bars, _) in enumerate(top, 1):
        if progress:
            progress(i, len(top), symbol)
        closes = pd.Series([b["c"] for b in bars])
        rsi = latest_rsi(closes)
        if rsi is None:
            continue
        out.append({"symbol": symbol, "price": closes.iloc[-1], "rsi": rsi})
    return out
