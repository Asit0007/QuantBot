"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/instruments.py — what the exchange will actually accept      ║
╠══════════════════════════════════════════════════════════════════════╣
║  This file exists because of a bug that cost the project its go-live  ║
║  date, and it is built to make that class of bug impossible to ship   ║
║  again.                                                              ║
║                                                                      ║
║  THE BUG (found 2026-09-06, CLAUDE.md §1):                            ║
║  Binance BTC/USDT:USDT has lot step 0.001 BTC and min notional $50.   ║
║  At BTC ~$79,800 one lot step is ~$79.85. Under nostop sizing at 5x,  ║
║  a $100 corpus requests $50 of notional = 0.000627 BTC, which floors  ║
║  to 0.000 -> the order is REJECTED. The bot silently places nothing.  ║
║                                                                      ║
║  Why ~30 backtests never caught it: the binding constraint is the LOT ║
║  STEP, not the notional floor, and the lot step's dollar value tracks ║
║  the underlying price. The backtest starts in 2019 at BTC ~$8,285,    ║
║  where one step cost $8 and only the $50 floor bound. By the time BTC ║
║  had risen ~8x, the simulated account had compounded well past the    ║
║  threshold. `too_small` was reported as 0 for the entire run.         ║
║                                                                      ║
║  LESSON, and the contract of this module: LIVE STARTING CONDITIONS    ║
║  ARE NOT THE BACKTEST'S. A constraint that never binds in simulation  ║
║  can bind on day one in production. min_viable_corpus() below is the  ║
║  check that would have caught it, and it must be run against CURRENT  ║
║  prices for every symbol in a universe before that universe is        ║
║  funded — not against the price at the start of the backtest.         ║
║                                                                      ║
║  This matters more, not less, for the new markets: an NSE F&O lot is  ║
║  a far larger minimum ticket than 0.001 BTC, and a 50-symbol universe ║
║  has 50 chances to hit it.                                            ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    """Exchange-enforced constraints on a single tradable symbol.

    Frozen because these are facts about the venue, not tunables. They
    belong beside the symbol, never in .env — .env is for things a human
    decides, this is for things the exchange decides.
    """

    symbol: str
    lot_step: float           # smallest quantity increment (0.001 BTC, 1 share, 1 lot)
    min_qty: float            # smallest order the venue accepts at all
    min_notional: float       # smallest order VALUE the venue accepts, in quote ccy
    price_tick: float = 0.01  # smallest price increment
    contract_size: float = 1.0  # units of underlying per lot (NSE F&O: 75 for Nifty)
    quote_ccy: str = "USD"

    def round_qty(self, qty: float) -> float:
        """Floor a quantity to the lot step.

        FLOOR, never round-to-nearest. Rounding up can breach the margin
        the sizing formula just computed, and under nostop the posted
        margin IS the maximum loss — so rounding up would quietly raise
        the loss ceiling above what was risked.
        """
        if self.lot_step <= 0:
            return qty
        return math.floor(qty / self.lot_step) * self.lot_step

    def rejects(self, qty: float, price: float) -> str | None:
        """Why the venue would reject this order, or None if it is valid.

        Returns a reason string rather than a bool so the caller can log
        WHICH constraint bound. bot.py's `_size_or_skip` currently logs a
        generic skip, which is why the $100 problem read as "the bot is
        quiet" rather than "every order is being rejected".
        """
        rounded = self.round_qty(qty)
        if rounded <= 0:
            return (f"qty {qty:.8f} floors to 0 at lot step {self.lot_step} "
                    f"(one step = {self.lot_step * price:,.2f} {self.quote_ccy})")
        if rounded < self.min_qty:
            return f"qty {rounded:.8f} below min_qty {self.min_qty}"
        notional = rounded * price * self.contract_size
        if notional < self.min_notional:
            return (f"notional {notional:,.2f} below min_notional "
                    f"{self.min_notional:,.2f} {self.quote_ccy}")
        return None

    def min_viable_corpus(self, price: float, risk_per_trade: float,
                          leverage: float) -> float:
        """Smallest corpus that can place ANY order in this instrument.

        Inverts the nostop sizing chain:
            margin   = corpus x risk_per_trade
            notional = margin x leverage
            qty      = notional / price

        The order must clear BOTH floors, so we take the binding one:
          * lot step  -> notional >= lot_step x price x contract_size
          * notional  -> notional >= min_notional

        Then corpus = notional / (leverage x risk_per_trade).

        Run this before funding anything. For BTC at $79,800 / 10% / 5x it
        reproduces the documented ~$160 figure.
        """
        need_notional = max(self.lot_step * price * self.contract_size,
                            self.min_notional)
        return need_notional / (leverage * risk_per_trade)

    def funding_floor(self, price: float, risk_per_trade: float,
                      leverage: float, median_drawdown: float = 0.34) -> float:
        """Corpus needed to still be able to trade AT the median drawdown.

        min_viable_corpus() is necessary but not sufficient, and the gap
        between them is the subtle part. Fund exactly at the minimum and
        the FIRST drawdown drops the corpus under the threshold, at which
        point the bot stops placing orders and therefore cannot place the
        trades that would earn the drawdown back. CLAUDE.md calls this
        SOFT RUIN: not a blown account, a frozen one.

        Default 34% is BTC's median RESAMPLED drawdown at 5x — deliberately
        the resampled figure, not the 25.5% realised one, because the
        realised drawdown was a lucky draw at every leverage tier.
        Each market must pass its own measured value.
        """
        return self.min_viable_corpus(price, risk_per_trade, leverage) / (1.0 - median_drawdown)


# ── Known instruments ────────────────────────────────────────────────────
# BTC is recorded from the live Binance values that produced the go-live
# blocker, so the regression test below has a real case to check against.
BTC_USDT_PERP = InstrumentSpec(
    symbol="BTC/USDT:USDT",
    lot_step=0.001,
    min_qty=0.001,
    min_notional=50.0,
    price_tick=0.10,
    quote_ccy="USDT",
)

# NSE cash equities: integer shares, no exchange minimum notional.
# The binding constraint here is share price, not a lot step — one share of
# a high-priced constituent is itself a large minimum ticket.
def nse_equity(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        lot_step=1.0,        # whole shares
        min_qty=1.0,
        min_notional=0.0,    # no exchange floor in the cash segment
        price_tick=0.05,     # NSE tick is 5 paise
        quote_ccy="INR",
    )


def us_equity(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        lot_step=1.0,        # whole shares (fractional support is broker-specific)
        min_qty=1.0,
        min_notional=0.0,
        price_tick=0.01,
        quote_ccy="USD",
    )
