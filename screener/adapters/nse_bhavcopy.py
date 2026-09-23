"""
NSE bhavcopy adapter — daily OHLCV + turnover for every listed instrument.

Purpose: source of (a) the price series RSI is computed on, and (b) the
turnover ranking used to bound the equity universe to something Screener.in
can be scraped for without hammering it. Free, no account, no API key.

Constraints:
- Only the UDiFF format is implemented (works 2024-01 onward). This tool only
  ever looks back ~2 months for a turnover/RSI window, so the legacy
  pre-2024 format used for multi-year backtests elsewhere in this workspace
  is out of scope here — don't add it without a reason.
- NSE requires a browser User-Agent and a Referer header; a bare `requests`
  call with no headers gets rejected.
- A 404 usually means a market holiday, not missing data — callers should
  treat gaps as normal, not raise.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "bhavcopy"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}
UDIFF_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
)

# SctySrs -> tier. Anything not listed here is out of scope for this tool
# (SME board, trade-for-trade surveillance, government/state bonds, T-bills —
# none of them are equity, ETF, REIT/InvIT or SGB). Verified against a live
# bhavcopy 2026-09-16; re-check if NSE ever adds a new series code.
TIER_BY_SERIES = {
    "EQ": "EQUITY_OR_ETF",  # split further by name — see classify()
    "RR": "REIT",
    "IV": "INVIT",
    "GB": "SGB",
}


def _cache_path(date: dt.date) -> Path:
    return CACHE_DIR / f"{date:%Y%m%d}.parquet"


def fetch_day(date: dt.date) -> pd.DataFrame | None:
    """One day's bhavcopy, every listed instrument. None on holiday/weekend/404."""
    cache = _cache_path(date)
    if cache.exists():
        return pd.read_parquet(cache)

    url = UDIFF_URL.format(date=f"{date:%Y%m%d}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return None

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(csv_name) as f:
            df = pd.read_csv(f)

    df = df[["TckrSymb", "SctySrs", "FinInstrmNm", "ClsPric", "TtlTrfVal", "TtlTradgVol"]].copy()
    df.columns = ["symbol", "series", "name", "close", "turnover", "volume"]
    df["date"] = date

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache)
    return df


def fetch_window(end_date: dt.date, trading_days: int = 40, max_calendar_days: int = 75) -> dict[dt.date, pd.DataFrame]:
    """Walk backward from end_date collecting up to `trading_days` sessions.

    max_calendar_days is a safety cap so a bad end_date (e.g. a long
    shutdown) can't turn this into an unbounded loop of 404s.
    """
    out: dict[dt.date, pd.DataFrame] = {}
    d = end_date
    checked = 0
    while len(out) < trading_days and checked < max_calendar_days:
        if d.weekday() < 5:  # skip Sat/Sun outright; NSE holidays just 404
            day_df = fetch_day(d)
            if day_df is not None:
                out[d] = day_df
        d -= dt.timedelta(days=1)
        checked += 1
    return out


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """Add a `tier` column: EQUITY / ETF / REIT / INVIT / SGB / EXCLUDE.

    ETF detection is a name heuristic (`ETF` in FinInstrmNm) rather than a
    series code — NSE files ETFs under the same "EQ" series as ordinary
    companies (verified 2026-09-16: 99/2652 EQ rows carry "ETF" in the name).
    """
    tier = df["series"].map(TIER_BY_SERIES).fillna("EXCLUDE")
    is_etf = df["name"].str.contains("ETF", case=False, na=False)
    tier = tier.where(~((tier == "EQUITY_OR_ETF") & is_etf), "ETF")
    tier = tier.where(tier != "EQUITY_OR_ETF", "EQUITY")
    out = df.copy()
    out["tier"] = tier
    return out


def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    # A handful of bond STRIPS (excluded tiers) share a TckrSymb across
    # legs (e.g. IMC1 as N1/N2/N3) — irrelevant to any tier this tool
    # screens, but they collide on a symbol-indexed reindex if left in.
    return df.drop_duplicates(subset="symbol", keep="first")


def build_price_panel(window: dict[dt.date, pd.DataFrame]) -> pd.DataFrame:
    """symbol x date close-price matrix, sorted by date ascending."""
    frames = [_dedupe(df).set_index("symbol")["close"].rename(date) for date, df in sorted(window.items())]
    panel = pd.concat(frames, axis=1)
    return panel


def average_turnover(window: dict[dt.date, pd.DataFrame]) -> pd.Series:
    """Mean daily traded value (Rs) per symbol over the window — the ranking key."""
    frames = [_dedupe(df).set_index("symbol")["turnover"] for df in window.values()]
    return pd.concat(frames, axis=1).mean(axis=1).sort_values(ascending=False)
