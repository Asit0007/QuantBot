"""
Builds today's instrument universe from bhavcopy, split into tiers.

NSE equities are bounded to the top EQUITY_TOP_N by average daily turnover;
every other NSE tier (ETF, REIT, InvIT, SGB) shares UNIVERSE_TOP_N — split
into two knobs on 2026-09-17 since equities are sized independently of the
rest. Both are reproducible, index-committee-independent cuts (the same
reasoning the workspace already settled on for NSE research: see the
nse-bhavcopy-is-the-data-source memory), and for equities it's what keeps
the Screener.in scrape to a polite size. A tier smaller than its cap (e.g.
6 REITs) is unaffected — a cap only ever removes names, never pads the
list. SME/T2T/surveillance series (SM, BE, BZ, ST, ...) and government
bonds (GS, SG, TB) are excluded entirely — see nse_bhavcopy.TIER_BY_SERIES.
"""

from __future__ import annotations

import datetime as dt

from screener.adapters import nse_bhavcopy
from screener import config


def build(
    end_date: dt.date | None = None,
    equity_top_n: int | None = None,
    other_top_n: int | None = None,
) -> dict:
    end_date = end_date or dt.date.today()
    equity_top_n = equity_top_n or config.EQUITY_TOP_N
    other_top_n = other_top_n or config.UNIVERSE_TOP_N

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
        "equity": top_symbols("EQUITY", equity_top_n),
        # Narrowed to Gold ETFs only (config.ETF_NAME_FILTER) — matched
        # against the full instrument name, same basis the ETF/equity split
        # itself uses, not the ticker (a ticker-only match would miss a
        # gold fund whose ticker doesn't happen to spell "GOLD") — then
        # other_top_n applied same as every other non-equity tier.
        "etf": top_symbols("ETF", other_top_n, name_contains=config.ETF_NAME_FILTER or None),
        "reit": top_symbols("REIT", other_top_n),
        "invit": top_symbols("INVIT", other_top_n),
        "sgb": top_symbols("SGB", other_top_n),
    }
