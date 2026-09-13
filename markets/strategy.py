"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/strategy.py — the pure arithmetic, parameterised by config   ║
╠══════════════════════════════════════════════════════════════════════╣
║  Same maths as bot.py's compute_indicators / size_position_nostop /   ║
║  liq_price / fit_to_margin, with the module-level globals replaced by ║
║  an explicit MarketConfig argument.                                   ║
║                                                                      ║
║  tests/test_strategy_equivalence.py feeds both implementations the    ║
║  SAME .env and the SAME candles and asserts the outputs match to the  ║
║  last float. That test is the contract: if it fails, this file is     ║
║  wrong, because bot.py is the reference implementation that a 6.9-    ║
║  year validated backtest was derived from.                            ║
║                                                                      ║
║  ── THE ONE DELIBERATE DIFFERENCE ────────────────────────────────── ║
║  require_exit_model_safe() REFUSES to size a nostop position on a     ║
║  market that gaps. This is new behaviour, and it is the single most   ║
║  important safety change in the multi-market work.                    ║
║                                                                      ║
║  The nostop model's entire premise is "the margin posted IS the       ║
║  maximum loss, because isolated-margin liquidation is a hard ceiling  ║
║  enforced by the exchange." That premise depends on price moving      ║
║  CONTINUOUSLY to the liquidation level, so the exchange can close at  ║
║  roughly that price. BTC trades 24/7, so it does.                     ║
║                                                                      ║
║  NSE is shut 15:30->09:15 plus weekends and holidays; NYSE likewise.  ║
║  An overnight gap opens THROUGH the liquidation price. The position   ║
║  is closed at the gap, not at the liquidation level, and the loss     ║
║  ceiling silently stops existing — the one property the whole model   ║
║  is built on. It fails exactly when it matters: on the overnight news ║
║  that caused the gap.                                                 ║
║                                                                      ║
║  So this is not a tuning preference to be overridden with a flag. It  ║
║  is a structural property of markets that close, and the guard raises ║
║  rather than warns for the same reason bot.py hard-fails on a missing ║
║  env var instead of defaulting.                                       ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD as MACDIndicator
from ta.volatility import AverageTrueRange

from .config import MarketConfig


class UnsafeExitModel(RuntimeError):
    """Raised when a config would remove a loss ceiling it cannot enforce."""


def require_exit_model_safe(cfg: MarketConfig, exit_model: str) -> None:
    """Refuse nostop on a gapping market. See the module docstring."""
    if exit_model == "nostop" and cfg.gaps_overnight:
        raise UnsafeExitModel(
            f"EXIT_MODEL=nostop is not valid for market '{cfg.market_id}': it "
            f"closes overnight (gaps_overnight=True).\n\n"
            f"nostop has no stop order. Its only loss ceiling is isolated-margin "
            f"liquidation, which requires price to move CONTINUOUSLY to the "
            f"liquidation level. A market that closes can gap straight through "
            f"it overnight, so the position closes at the gap and the loss is "
            f"unbounded by the margin posted.\n\n"
            f"Use a real stop for this market. Do not override this check."
        )


# ══════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame, cfg: MarketConfig) -> pd.DataFrame:
    """Mirror of bot.py:328, with cfg replacing the module globals.

    NOTE this is a ROLLING min/max divergence, not pivot-based swing
    detection. That distinction is load-bearing history: the original
    swing detector marked a swing at bar i using bars i+1..i+5, which is
    lookahead bias, and it produced a $2.4 BILLION backtest. The rolling
    form here cannot see the future by construction. Do not "improve" it
    into pivot detection without re-deriving the whole result.
    """
    df = df.copy()

    df["rsi"] = RSIIndicator(close=df["close"], window=cfg.rsi_len).rsi()

    macd = MACDIndicator(
        close=df["close"],
        window_fast=cfg.macd_fast,
        window_slow=cfg.macd_slow,
        window_sign=cfg.macd_signal_win,
    )
    df["macd_line"] = macd.macd()
    df["signal_line"] = macd.macd_signal()

    df["avg_vol"] = df["volume"].rolling(cfg.vol_sma_period).mean()
    df["high_vol"] = df["volume"] > (cfg.vol_mult * df["avg_vol"])

    df["atr"] = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=cfg.atr_period
    ).average_true_range()

    df["low_close"] = df["close"].rolling(cfg.div_window).min()
    df["low_rsi"] = df["rsi"].rolling(cfg.div_window).min()
    df["high_close"] = df["close"].rolling(cfg.div_window).max()
    df["high_rsi"] = df["rsi"].rolling(cfg.div_window).max()

    df["bull_div"] = (
        (df["low_close"] < df["low_close"].shift(cfg.div_shift))
        & (df["low_rsi"] > df["low_rsi"].shift(cfg.div_shift))
    )
    df["bear_div"] = (
        (df["high_close"] > df["high_close"].shift(cfg.div_shift))
        & (df["high_rsi"] < df["high_rsi"].shift(cfg.div_shift))
    )

    pm = df["macd_line"].shift(1)
    ps = df["signal_line"].shift(1)
    df["macd_bull_cross"] = (df["macd_line"] > df["signal_line"]) & (pm <= ps)
    df["macd_bear_cross"] = (df["macd_line"] < df["signal_line"]) & (pm >= ps)

    return df


def session_relative_volume(df: pd.DataFrame, cfg: MarketConfig,
                            bar_index: pd.Series, lookback_days: int = 20) -> pd.Series:
    """Volume spike flag for SESSION markets — replaces the flat SMA gate.

    On a 24/7 market `volume > 2x its 20-bar SMA` is a real signal. On a
    session market it is a clock reading: intraday volume is U-shaped,
    spiking at the open and again at the close EVERY day, so a flat rolling
    mean is exceeded at 09:15 and 15:30 in perpetuity. The gate would fire
    on the calendar rather than on information, and a strategy built on it
    would be trading the opening auction's shape, not a signal.

    Fix: compare each bar against the same BAR INDEX on prior days, so the
    open is measured against other opens.
    """
    out = pd.Series(False, index=df.index)
    for idx in bar_index.unique():
        if idx < 0:
            continue
        mask = bar_index == idx
        same_slot = df.loc[mask, "volume"]
        baseline = same_slot.rolling(lookback_days, min_periods=5).mean().shift(1)
        out.loc[mask] = same_slot > (cfg.vol_mult * baseline)
    return out.fillna(False)


# ══════════════════════════════════════════════════════════════════════
#  SIZING
# ══════════════════════════════════════════════════════════════════════

def liq_price(side: str, entry: float, cfg: MarketConfig) -> float:
    """Isolated-margin liquidation price, one-way mode. Mirror of bot.py:674.

    MONITORING ONLY — the exchange computes the real one and is the only
    authority. The approximation errs in a known direction: funding is
    charged against an isolated position's own margin, walking the true
    liquidation CLOSER as a position ages, so the real level is hit
    slightly sooner than this returns, never later.
    """
    imr = 1.0 / cfg.leverage
    if side == "long":
        return entry * (1 - imr + cfg.maint_margin_rate)
    return entry * (1 + imr - cfg.maint_margin_rate)


def size_position_nostop(corpus: float, price: float, cfg: MarketConfig) -> dict:
    """Margin-first sizing. Mirror of bot.py:698.

        margin   = corpus x risk_per_trade   <- IS the maximum loss
        notional = margin x leverage
        qty      = notional / price

    LEVERAGE APPEARS IN NEITHER qty NOR P&L. Real futures P&L is
    `delta_price x qty`; leverage only decides how much margin the exchange
    locks and where liquidation sits. Getting this wrong is what produced
    the $9,347 fiction — the engine multiplied P&L by leverage while
    dividing size by it, and the two errors cancelled exactly for
    loss-at-stop, which is why ~20 backtests never noticed.
    """
    require_exit_model_safe(cfg, "nostop")
    margin = corpus * cfg.risk_per_trade
    notional = margin * cfg.leverage
    qty = notional / price
    return {"margin": margin, "notional": notional, "qty": qty,
            "stop_distance": 0.0, "dollar_risk": margin}


def size_position_stop(corpus: float, price: float, stop_price: float,
                       cfg: MarketConfig) -> dict:
    """Stop-distance sizing — the model equities MUST use. Mirror of bot.py:614.

        qty      = (corpus x risk_per_trade) / stop_distance
        notional = qty x price
        margin   = notional / leverage

    Note the consequence nostop does not have: margin/corpus =
    risk / (stop_pct x leverage), which runs to ~1.0x corpus for a 2% stop
    at 5x. Every trade posts most of the account, and fees scale with that
    notional — which is why the stop model's 6.9-year fees were $581
    against nostop's $64. Budget for it rather than being surprised by it.
    """
    stop_distance = abs(price - stop_price)
    if stop_distance <= 0:
        raise ValueError("stop_distance must be > 0 for stop-based sizing")
    qty = (corpus * cfg.risk_per_trade) / stop_distance
    notional = qty * price
    return {"margin": notional / cfg.leverage, "notional": notional, "qty": qty,
            "stop_distance": stop_distance, "dollar_risk": corpus * cfg.risk_per_trade}


def fit_to_margin(sizing: dict, free_balance: float, cfg: MarketConfig) -> dict:
    """Shrink a position until its margin fits the balance. Mirror of bot.py:731.

    Policy is to scale down rather than skip: a trader with $50 free takes
    the trade smaller, they do not sit it out. Scaling is proportional
    across qty/notional/margin/risk, so the nostop "max loss = margin"
    identity survives being shrunk.
    """
    cap = free_balance * cfg.max_margin_frac
    if cap <= 0 or sizing["margin"] <= cap:
        return {**sizing, "scaled_to": 1.0}
    ratio = cap / sizing["margin"]
    return {
        **sizing,
        "qty": sizing["qty"] * ratio,
        "notional": sizing["notional"] * ratio,
        "margin": cap,
        "dollar_risk": sizing["dollar_risk"] * ratio,
        "scaled_to": ratio,
    }
