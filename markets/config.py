"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/config.py — per-market configuration as a VALUE             ║
╠══════════════════════════════════════════════════════════════════════╣
║  bot.py reads every tunable into a module-level global at import      ║
║  time (bot.py:66-201): SYMBOL, LEVERAGE, RISK_PER_TRADE, RSI_LEN and  ║
║  ~25 more. The functions then close over those globals directly.      ║
║                                                                      ║
║  That is a perfectly reasonable design for one bot, and it is a hard  ║
║  wall for two: one process can hold exactly one market's config,      ║
║  permanently. You cannot run NSE and BTC in the same interpreter, you ║
║  cannot backtest two markets in one script, and you cannot unit-test  ║
║  a sizing function against anything but whatever .env happens to say. ║
║                                                                      ║
║  Passing config as a value fixes all three. It is also what makes the ║
║  equivalence test in tests/test_strategy_equivalence.py possible:     ║
║  build a MarketConfig from the SAME .env bot.py loaded, and any       ║
║  difference in output is a real difference in arithmetic rather than  ║
║  a difference in configuration.                                       ║
║                                                                      ║
║  NOTE the deliberate omission: there is no EXIT_MODEL="nostop" option ║
║  for a gapping market. See markets/strategy.py for why that is a      ║
║  safety property and not an oversight.                                ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MarketConfig:
    """Everything a market's engine needs to know, as one immutable value.

    Frozen on purpose. bot.py's globals are mutable in principle, and the
    replay harness needs absolute confidence that nothing reassigned
    LEVERAGE halfway through 6.9 years of candles.
    """

    market_id: str
    symbols: tuple[str, ...]
    timeframe: str
    candle_minutes: int

    # ── Risk / sizing ────────────────────────────────────────────────
    leverage: float = 5.0
    risk_per_trade: float = 0.10
    max_margin_frac: float = 1.0
    maint_margin_rate: float = 0.004
    fee_rate: float = 0.0005

    # ── Signal (the LOCKED BTC values are the defaults) ──────────────
    rsi_len: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal_win: int = 9
    vol_mult: float = 2.0
    vol_sma_period: int = 20
    atr_period: int = 14
    div_window: int = 5
    div_shift: int = 5
    div_memory: int = 3

    # ── Circuit breaker ──────────────────────────────────────────────
    cb_trigger: int = 5
    cb_hours: int = 48

    # ── DCA ──────────────────────────────────────────────────────────
    dca_day: int = 10
    dca_monthly: float = 10.0
    dca_annual_growth: float = 0.10
    start_year: int = 2026

    # ── Data ─────────────────────────────────────────────────────────
    candles_needed: int = 200
    warmup: int = 50

    # ── Multi-symbol universes only ──────────────────────────────────
    # BTC never needed this: one symbol can only ever have one position.
    # A 50-symbol Nifty universe on a small corpus will fire more signals
    # than it can fund, so the engine needs a hard cap AND a deterministic
    # rule for which of the competing signals to take. Ranking on anything
    # computed after the bar closes would reintroduce lookahead bias
    # through the back door, so the rule must be pre-committed.
    max_concurrent_positions: int = 1
    signal_ranking: str = "alphabetical"   # the honest null hypothesis

    # ── Session semantics ────────────────────────────────────────────
    # True for any market that closes. Under a gapping market the nostop
    # model's loss ceiling does not hold (see markets/strategy.py), so this
    # flag is a safety interlock, not a display preference.
    gaps_overnight: bool = False

    benchmarks: dict = field(default_factory=dict)

    @property
    def is_universe(self) -> bool:
        return len(self.symbols) > 1

    @classmethod
    def from_env(cls, market_id: str, symbols=None, **overrides) -> "MarketConfig":
        """Build from environment variables, matching bot.py's key names.

        Reads the same keys bot.py does so a MarketConfig can be built from
        the exact .env the live bot loaded — which is precisely what the
        equivalence test needs in order to be meaningful.
        """
        def _f(key, default):
            v = os.getenv(key)
            return default if v is None else float(v)

        def _i(key, default):
            v = os.getenv(key)
            return default if v is None else int(v)

        syms = tuple(symbols) if symbols else (os.getenv("SYMBOL", "BTC/USDT"),)
        base = dict(
            market_id=market_id,
            symbols=syms,
            timeframe=os.getenv("TIMEFRAME", "15m"),
            candle_minutes=_i("CANDLE_MINUTES", 15),
            leverage=_f("LEVERAGE", 5.0),
            risk_per_trade=_f("RISK_PER_TRADE", 0.10),
            max_margin_frac=_f("MAX_MARGIN_FRAC", 1.0),
            maint_margin_rate=_f("MAINT_MARGIN_RATE", 0.004),
            fee_rate=_f("FEE_RATE", 0.0005),
            rsi_len=_i("RSI_LEN", 14),
            macd_fast=_i("MACD_FAST", 12),
            macd_slow=_i("MACD_SLOW", 26),
            macd_signal_win=_i("MACD_SIGNAL_WIN", 9),
            vol_mult=_f("VOL_MULT", 2.0),
            vol_sma_period=_i("VOL_SMA_PERIOD", 20),
            atr_period=_i("ATR_PERIOD", 14),
            div_window=_i("DIV_WINDOW", 5),
            div_shift=_i("DIV_SHIFT", 5),
            div_memory=_i("DIV_MEMORY", 3),
            cb_trigger=_i("CB_TRIGGER", 5),
            cb_hours=_i("CB_HOURS", 48),
            dca_day=_i("DCA_DAY", 10),
            dca_monthly=_f("DCA_MONTHLY_USD", 10.0),
            dca_annual_growth=_f("DCA_ANNUAL_GROWTH", 0.10),
            start_year=_i("START_YEAR", 2026),
            candles_needed=_i("CANDLES_NEEDED", 200),
            warmup=_i("WARMUP", 50),
        )
        base.update(overrides)
        return cls(**base)
