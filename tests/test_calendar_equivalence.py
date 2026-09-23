"""
Proves AlwaysOpenCalendar.secs_to_next_candle() is bit-for-bit identical to
bot.py's secs_to_next_candle(), across a wide sweep of clock times and
candle sizes.

WHY THIS TEST IS THE IMPORTANT ONE
----------------------------------
The multi-market refactor's first job is to not change BTC. bot.py is live
code trading (paper, for now) against a validated backtest, and the whole
go-live gate is a paper-vs-backtest comparison — which means nothing if the
engine underneath quietly drifted. Every other new market is a place where
the right answer is unknown; BTC is the one place it is known, so it is the
only place the abstraction can actually be verified.

If this test fails, markets/calendar.py is wrong. Do not "fix" it by
relaxing the assertion.

Run: ./venv/bin/python tests/test_calendar_equivalence.py
"""
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# bot.py hard-fails at import if the env is incomplete, and makedirs()es
# DATA_DIR. Point that at a throwaway dir so importing it for a test can
# never touch the real state files — /app/data in a deployed .env would
# also fail outright on macOS.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="quantbot-test-")

import bot                                    # noqa: E402
from markets.calendar import AlwaysOpenCalendar   # noqa: E402


class _FrozenDatetime(datetime):
    """Stands in for bot.datetime so bot.py's now() is controllable.

    bot.py's secs_to_next_candle() calls datetime.now(timezone.utc)
    internally, so the only way to feed both implementations the same
    instant is to freeze the module's clock. markets/calendar.py takes
    `now` as a parameter precisely so it never needs this.
    """
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


def main() -> int:
    cal = AlwaysOpenCalendar()
    real_datetime = bot.datetime
    bot.datetime = _FrozenDatetime

    random.seed(20260913)          # deterministic: a failure must reproduce
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Random instants, plus boundary cases a random sweep is unlikely to hit:
    # exact candle boundaries, one second either side, and end-of-hour.
    moments = [base + timedelta(seconds=random.randint(0, 366 * 24 * 3600))
               for _ in range(20000)]
    for m in (0, 1, 14, 15, 29, 30, 44, 45, 59):
        for s in (0, 1, 30, 59):
            moments.append(base.replace(minute=m, second=s))

    checked = 0
    failures = []

    for cm in (1, 3, 5, 15, 30):
        bot.CANDLE_MINUTES = cm
        for now in moments:
            _FrozenDatetime._frozen = now
            expected = bot.secs_to_next_candle()
            actual = cal.secs_to_next_candle(now, cm)
            checked += 1
            if expected != actual:
                failures.append((cm, now.isoformat(), expected, actual))
                if len(failures) >= 5:
                    break
        if failures:
            break

    bot.datetime = real_datetime

    print(f"compared {checked:,} (time x candle_minutes) combinations")
    if failures:
        print("\nFAIL — AlwaysOpenCalendar diverges from bot.py:")
        for cm, ts, exp, act in failures:
            print(f"  candle_minutes={cm} at {ts}: bot.py={exp} calendar={act}")
        return 1

    print("PASS — AlwaysOpenCalendar is identical to bot.py's clock")
    print("       BTC behaviour is unchanged by the market abstraction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
