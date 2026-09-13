"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/base.py — the seam between strategy and venue               ║
╠══════════════════════════════════════════════════════════════════════╣
║  bot.py already has this seam and it is a clean one: every network    ║
║  call lives inside `class Exchange` (bot.py:426-607) and QuantBot     ║
║  touches the venue only through self.ex.<method>. Eleven methods, one ║
║  boundary. That is the single reason a multi-market refactor is       ║
║  tractable at all rather than a rewrite.                              ║
║                                                                      ║
║  DataAdapter below is that same surface, stated as a Protocol so an   ║
║  NSE or US implementation can be checked against it rather than       ║
║  discovered to be incomplete at runtime, halfway through a session.   ║
║                                                                      ║
║  TWO THINGS THAT DO NOT GENERALISE, and are therefore optional:       ║
║                                                                      ║
║  * funding_rate() — perpetual funding settled at 00/08/16 UTC is a    ║
║    crypto-perp concept with no equity analogue. Equities have         ║
║    dividends, borrow and roll costs instead: different sign,          ║
║    different cadence, different accounting. An equity adapter should  ║
║    not implement this; it should implement carry_cost() when the      ║
║    research reaches the point of needing one.                         ║
║                                                                      ║
║  * configure_leverage() — Binance takes a leverage setting per        ║
║    symbol. NSE F&O uses SPAN + exposure margin computed by the        ║
║    exchange, and US cash equities use Reg-T. There is no number to    ║
║    set, so the default is a no-op rather than a lie.                  ║
║                                                                      ║
║  ERROR TAXONOMY: bot.py's main loop catches ccxt.NetworkError and     ║
║  ccxt.ExchangeError by name (bot.py:1522-1528), so a non-ccxt adapter ║
║  would fall through to the bare 60s retry and lose the distinction    ║
║  between "retry in 30s" and "retry in 60s". AdapterNetworkError and   ║
║  AdapterVenueError exist so every adapter can raise the same two      ║
║  categories regardless of what library it wraps.                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from .calendar import MarketCalendar
from .instruments import InstrumentSpec


class AdapterError(RuntimeError):
    """Base for every venue-facing failure."""


class AdapterNetworkError(AdapterError):
    """Transient — connectivity, timeout, rate limit. Retry shortly.

    Maps to ccxt.NetworkError. MUST NOT be raised for a venue rejecting an
    order: that is a permanent condition and retrying it is how a bot ends
    up hammering an endpoint that will never say yes.
    """


class AdapterVenueError(AdapterError):
    """The venue answered, and the answer was no. Back off further."""


@runtime_checkable
class DataAdapter(Protocol):
    """Everything the engine needs from a venue. Mirrors bot.py's Exchange.

    Deliberately NOT an ABC. A Protocol lets an adapter be validated
    structurally without inheriting, which keeps the crypto adapter free to
    stay a thin pass-through to the existing ccxt object rather than being
    re-parented into a hierarchy it does not need.
    """

    calendar: MarketCalendar

    def connect(self) -> None:
        """Resolve symbols and load venue metadata. Raise if unusable."""
        ...

    def fetch_candles(self, symbol: str, limit: int) -> pd.DataFrame:
        """CLOSED candles only, ascending, indexed by UTC timestamp.

        Columns: open, high, low, close, volume.

        "Closed only" is not a detail. bot.py drops the forming candle with
        df.iloc[:-1] and dedupes on last_candle_ts; an adapter that returns
        a partial bar puts a moving number into an indicator, and the
        signal changes under the engine mid-bar.
        """
        ...

    def current_price(self, symbol: str) -> float:
        """Last traded price. Used for paper fills and monitoring."""
        ...

    def instrument(self, symbol: str) -> InstrumentSpec:
        """Venue constraints for this symbol.

        Must reflect what the venue enforces TODAY, not what it enforced
        when a backtest was run — the lot step's dollar value tracks the
        underlying, which is exactly how the $100 go-live blocker hid from
        ~30 backtests. See markets/instruments.py.
        """
        ...

    def get_position(self, symbol: str) -> dict | None:
        """Live position as the VENUE sees it, or None if flat.

        Reconciliation depends on this being authoritative. Any API error
        must RAISE, never return None — "I could not reach the exchange"
        and "there is no position" are opposite facts, and conflating them
        is how a phantom position persists indefinitely.
        """
        ...


@runtime_checkable
class TradingAdapter(DataAdapter, Protocol):
    """A DataAdapter that can also place orders.

    Split from DataAdapter on purpose: everything in Phases 0-6 is research
    and paper only, so those adapters implement the read half and CANNOT
    place an order even by accident. An adapter gains order methods when
    someone deliberately writes them, which is a much better gate than a
    PAPER_TRADE boolean that one typo can flip.
    """

    def place_entry(self, symbol: str, side: str, qty: float,
                    stop: float | None = None) -> dict: ...

    def place_exit(self, symbol: str, side: str, qty: float) -> dict: ...

    def configure_leverage(self, symbol: str, leverage: float) -> None:
        """No-op where the venue has no leverage setting (NSE SPAN, Reg-T)."""
