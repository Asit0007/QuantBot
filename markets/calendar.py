"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/calendar.py — WHEN does a market have a new closed candle?   ║
╠══════════════════════════════════════════════════════════════════════╣
║  This module exists because bot.py:1359 answers that question with:   ║
║                                                                      ║
║      elapsed = (now.minute % CANDLE_MINUTES) * 60 + now.second        ║
║      return (CANDLE_MINUTES * 60 - elapsed) + 6                       ║
║                                                                      ║
║  A pure UTC minute-modulo. It is correct, and it is correct ONLY      ║
║  because BTC trades 24/7. It silently assumes three things:           ║
║                                                                      ║
║    1. candles align to UTC midnight,                                  ║
║    2. the market never closes,                                        ║
║    3. CANDLE_MINUTES < 60.                                            ║
║                                                                      ║
║  For NSE (09:15-15:30 IST) and NYSE (09:30-16:00 ET) all three are    ║
║  wrong. A session market's bars are aligned to the SESSION OPEN, not  ║
║  to UTC midnight: NSE's 15m grid runs 09:30, 09:45, ... from a 09:15  ║
║  open. Run the modulo version against NSE and the bot spins all night ║
║  and all weekend re-fetching a stale final candle, and its bar        ║
║  boundaries are offset from the exchange's for any timeframe whose    ║
║  grid does not happen to divide the open.                             ║
║                                                                      ║
║  CONTRACT — the reason this file is testable:                         ║
║  AlwaysOpenCalendar.secs_to_next_candle() must return EXACTLY what    ║
║  bot.py's secs_to_next_candle() returns, for every input. That        ║
║  equality is asserted in tests/test_calendar_equivalence.py and it is ║
║  what proves this abstraction did not change BTC's behaviour. If you  ║
║  edit AlwaysOpenCalendar, that test is the thing that must still pass.║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta, timezone

# Buffer after a candle closes before we fetch it, in seconds. Carried over
# from bot.py verbatim: the exchange needs a moment to finalise the bar, and
# fetching at exactly the boundary can return the still-forming candle.
CANDLE_FETCH_BUFFER_S = 6


class MarketCalendar(ABC):
    """When is this market open, and when does its next candle close?

    Implementations must be PURE with respect to `now` — every method takes
    the timestamp explicitly rather than calling datetime.now() internally.
    bot.py calls datetime.now() inside secs_to_next_candle(), which makes it
    untestable without freezing the clock; that is a mistake not repeated
    here, and it is what lets the equivalence test inject identical inputs
    into both implementations.
    """

    name: str = "abstract"

    @abstractmethod
    def is_open(self, ts: datetime) -> bool:
        """Is the market trading at this instant?"""

    @abstractmethod
    def secs_to_next_candle(self, now: datetime, candle_minutes: int) -> float:
        """Seconds to wait before the next closed candle is fetchable."""

    @abstractmethod
    def session_bounds(self, day) -> tuple | None:
        """(open, close) for a calendar date, or None if it is not a trading day."""

    @abstractmethod
    def bar_index_in_session(self, ts: datetime, candle_minutes: int) -> int:
        """0-based index of this bar within its session.

        This is what makes session-relative volume normalisation possible.
        Intraday volume on an equity market is U-shaped — it spikes at the
        open and again at the close, every single day. A flat
        `volume > 2x 20-bar SMA` gate therefore fires on the CLOCK rather
        than on information, and would trigger at 09:15 and 15:30 forever.
        Comparing a bar against the same bar index on prior days removes
        that, which is why every adapter must supply this.
        """

    def gap_is_expected(self, t0: datetime, t1: datetime) -> bool:
        """Is the absence of data between t0 and t1 normal, or a real hole?

        ohlcv_cache.gap_report() has no calendar, so on a session market it
        flags every overnight and every weekend as a warning — thousands of
        them, which trains you to ignore the report entirely and therefore
        to miss the one gap that matters. Default is 24/7 semantics: any
        gap is a real gap.
        """
        return False


class AlwaysOpenCalendar(MarketCalendar):
    """24/7 markets — crypto. Bit-for-bit compatible with bot.py.

    Deliberately NOT implemented as "a session calendar whose session is the
    whole day". It reproduces bot.py's arithmetic literally, including its
    quirks, because its job is to be a provably behaviour-preserving stand-in
    for the live BTC path rather than a tidier re-derivation of it.
    """

    name = "always_open"

    def is_open(self, ts: datetime) -> bool:
        return True

    def secs_to_next_candle(self, now: datetime, candle_minutes: int) -> float:
        # Mirrors bot.py:1359 exactly, including the two properties below
        # that a "cleaner" rewrite would quietly change:
        #
        #   * microseconds are IGNORED (bot.py uses .second, not a full
        #     timedelta), so at 14:29:59.9 both return 6.1s-worth of wait
        #     rounded to whole seconds, not 6.0000001.
        #   * at exactly a boundary (elapsed == 0) it returns a FULL period
        #     plus the buffer, not the buffer alone — it waits for the NEXT
        #     candle rather than grabbing the one that just closed.
        #
        # Both are load-bearing for the equivalence test. Do not "fix" them
        # here; if they should change, change bot.py and re-run the replay.
        elapsed = (now.minute % candle_minutes) * 60 + now.second
        return (candle_minutes * 60 - elapsed) + CANDLE_FETCH_BUFFER_S

    def session_bounds(self, day) -> tuple:
        start = datetime.combine(day, time(0, 0), tzinfo=timezone.utc)
        return (start, start + timedelta(days=1))

    def bar_index_in_session(self, ts: datetime, candle_minutes: int) -> int:
        utc = ts.astimezone(timezone.utc)
        return (utc.hour * 60 + utc.minute) // candle_minutes

    def gap_is_expected(self, t0: datetime, t1: datetime) -> bool:
        return False        # 24/7: every gap is a real gap


class SessionCalendar(MarketCalendar):
    """Markets that close — NSE (XNSE), NYSE (XNYS), CME (CMES).

    CALENDAR CODES: there is NO "XNSE" in exchange_calendars — India is
    served by XBOM/BSE/XBSE only. NSE and BSE share one national trading
    calendar (same holidays, same 09:15-15:30 session), so XBOM is the
    correct proxy for NSE and not a compromise. Validated against reality
    rather than assumed: XBOM reports 2019-09-02 as a non-session, and
    NSE's own bhavcopy archive 404s that date (Ganesh Chaturthi) while
    serving 2019-09-03. Its session bounds come back as 03:45-10:00 UTC,
    which is 09:15-15:30 IST.

    COVERAGE FLOOR: XBOM's first session is 2006-09-13. The bhavcopy
    archive reaches back to 2000, so pre-2006 data has no calendar to
    validate it against — treat 2006-09-13 as the usable start of any
    NSE study rather than silently trusting six earlier years.

    Backed by `exchange_calendars` rather than a hand-rolled holiday table.
    Holiday data has a long tail (special sessions, half-days, one-off
    closures, muhurat trading on NSE) and getting it wrong does not raise —
    it silently produces plausible-looking wrong answers, which is the
    worst failure mode available to a backtest.

    BAR ALIGNMENT: bars run from the SESSION OPEN, not from UTC midnight.
    NSE opens 09:15 IST, so its 15m grid is 09:30, 09:45, ... and its 30m
    grid is 09:45, 10:15, ... Aligning to UTC midnight instead would offset
    every bar boundary from the exchange's own, which corrupts the candles
    before any strategy ever sees them.
    """

    name = "session"

    def __init__(self, xcal_code: str, candle_minutes_hint: int = 15):
        # Imported lazily so this module stays importable — and CI's
        # py_compile stays green — on a machine that has not installed the
        # research requirements. Production never imports this class.
        try:
            import exchange_calendars as xcals
        except ImportError as e:                       # pragma: no cover
            raise ImportError(
                "SessionCalendar needs `exchange_calendars`. It is deliberately "
                "NOT in requirements.txt (that file triggers a full production "
                "rebuild). Install it locally:\n"
                "    ./venv/bin/pip install -r requirements-research.txt"
            ) from e

        self._xcal = xcals.get_calendar(xcal_code)
        self.name = f"session:{xcal_code}"
        self._hint = candle_minutes_hint

    def is_open(self, ts: datetime) -> bool:
        return bool(self._xcal.is_open_on_minute(ts))

    def session_bounds(self, day) -> tuple | None:
        import pandas as pd
        ts = pd.Timestamp(day)
        if not self._xcal.is_session(ts.normalize()):
            return None
        return (self._xcal.session_open(ts.normalize()).to_pydatetime(),
                self._xcal.session_close(ts.normalize()).to_pydatetime())

    def secs_to_next_candle(self, now: datetime, candle_minutes: int) -> float:
        """Seconds until the next in-session bar close, plus the fetch buffer.

        Three cases, and the third is the one a naive implementation gets
        wrong: when the market is CLOSED we must wait for the first bar of
        the next session to close, which is `open + candle_minutes`, not the
        open itself — there is no closed candle at the instant of the open.
        """
        import pandas as pd

        bounds = self.session_bounds(now.date())
        if bounds and bounds[0] <= now < bounds[1]:
            s_open, s_close = bounds
            since_open = (now - s_open).total_seconds()
            period = candle_minutes * 60
            n_done = int(since_open // period)
            nxt = s_open + timedelta(seconds=(n_done + 1) * period)
            if nxt <= s_close:
                return (nxt - now).total_seconds() + CANDLE_FETCH_BUFFER_S
            # Past the last full bar of the day — fall through to next session.

        nxt_sess = self._xcal.next_open(pd.Timestamp(now)).to_pydatetime()
        first_close = nxt_sess + timedelta(minutes=candle_minutes)
        return (first_close - now).total_seconds() + CANDLE_FETCH_BUFFER_S

    def bar_index_in_session(self, ts: datetime, candle_minutes: int) -> int:
        bounds = self.session_bounds(ts.date())
        if not bounds:
            return -1
        return int((ts - bounds[0]).total_seconds() // (candle_minutes * 60))

    def gap_is_expected(self, t0: datetime, t1: datetime) -> bool:
        """True when every minute between t0 and t1 is outside a session.

        Lets ohlcv_cache's gap report stay useful on a market that is closed
        two thirds of every weekday and all weekend.
        """
        import pandas as pd
        mins = self._xcal.minutes_in_range(pd.Timestamp(t0), pd.Timestamp(t1))
        return len(mins) == 0


# ── Registry ─────────────────────────────────────────────────────────────
# Keyed by market id. Values are factories, not instances: building a
# SessionCalendar imports exchange_calendars and parses a holiday table, and
# doing that at import time would make this module fail on a box that only
# runs the crypto path.
CALENDARS = {
    "btc":        lambda: AlwaysOpenCalendar(),
    # XBOM, not XNSE — no NSE calendar exists; see SessionCalendar's docstring.
    "nse_stocks": lambda: SessionCalendar("XBOM"),
    "us_indices": lambda: SessionCalendar("XNYS"),
}


def get_calendar(market_id: str) -> MarketCalendar:
    if market_id not in CALENDARS:
        raise KeyError(
            f"Unknown market '{market_id}'. Known: {sorted(CALENDARS)}"
        )
    return CALENDARS[market_id]()
