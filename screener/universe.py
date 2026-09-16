"""
Builds today's instrument universe from bhavcopy, split into tiers.

Equity candidates are bounded to the top N by average daily turnover rather
than all ~2,600 EQ-series names — that's a reproducible, index-committee-
independent selection (the same reasoning the workspace already settled on
for NSE research: see the nse-bhavcopy-is-the-data-source memory), and it's
what keeps the Screener.in scrape to a polite size. SME/T2T/surveillance
series (SM, BE, BZ, ST, ...) and government bonds (GS, SG, TB) are excluded
entirely — see nse_bhavcopy.TIER_BY_SERIES.
"""

from __future__ import annotations

import datetime as dt

from screener.adapters import nse_bhavcopy
from screener import config


def build(end_date: dt.date | None = None, top_n: int | None = None) -> dict:
    end_date = end_date or dt.date.today()
    top_n = top_n or config.UNIVERSE_TOP_N

    window = nse_bhavcopy.fetch_window(end_date, trading_days=config.TURNOVER_LOOKBACK_DAYS)
    if not window:
        raise RuntimeError(f"No bhavcopy data found looking back from {end_date}")

    latest_date = max(window)
    latest = nse_bhavcopy.classify(window[latest_date])
    price_panel = nse_bhavcopy.build_price_panel(window)
    turnover = nse_bhavcopy.average_turnover(window)

    def top_symbols(tier: str, n: int | None = None) -> list[str]:
        symbols = latest.loc[latest["tier"] == tier, "symbol"]
        ranked = turnover.reindex(symbols).dropna().sort_values(ascending=False)
        return list(ranked.index[:n] if n else ranked.index)

    return {
        "as_of": latest_date,
        "price_panel": price_panel,  # symbol x date close, for RSI
        "equity": top_symbols("EQUITY", top_n),
        "etf": top_symbols("ETF"),
        "reit": top_symbols("REIT"),
        "invit": top_symbols("INVIT"),
        "sgb": top_symbols("SGB"),
    }
