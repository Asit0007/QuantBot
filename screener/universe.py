"""
Builds today's instrument universe from bhavcopy, split into tiers.

Every NSE tier (equity, ETF, REIT, InvIT, SGB) is bounded to the top
UNIVERSE_TOP_N by average daily turnover within that tier, rather than
reported in full — that's a reproducible, index-committee-independent
selection (the same reasoning the workspace already settled on for NSE
research: see the nse-bhavcopy-is-the-data-source memory), and for equities
it's what keeps the Screener.in scrape to a polite size. A tier smaller than
UNIVERSE_TOP_N (e.g. 6 REITs) is unaffected — the cap only ever removes
names, never pads the list. SME/T2T/surveillance series (SM, BE, BZ, ST,
...) and government bonds (GS, SG, TB) are excluded entirely — see
nse_bhavcopy.TIER_BY_SERIES.
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

    def top_symbols(tier: str, n: int | None = None, name_contains: str | None = None) -> list[str]:
        rows = latest[latest["tier"] == tier]
        if name_contains:
            rows = rows[rows["name"].str.contains(name_contains, case=False, na=False)]
        ranked = turnover.reindex(rows["symbol"]).dropna().sort_values(ascending=False)
        return list(ranked.index[:n] if n else ranked.index)

    return {
        "as_of": latest_date,
        "price_panel": price_panel,  # symbol x date close, for RSI
        "equity": top_symbols("EQUITY", top_n),
        # Narrowed to Gold ETFs only (config.ETF_NAME_FILTER) — matched
        # against the full instrument name, same basis the ETF/equity split
        # itself uses, not the ticker (a ticker-only match would miss a
        # gold fund whose ticker doesn't happen to spell "GOLD") — then
        # top_n applied same as every other tier.
        "etf": top_symbols("ETF", top_n, name_contains=config.ETF_NAME_FILTER or None),
        "reit": top_symbols("REIT", top_n),
        "invit": top_symbols("INVIT", top_n),
        "sgb": top_symbols("SGB", top_n),
    }
