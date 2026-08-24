#!/usr/bin/env python3
"""
bot.py — QuantBot Live Trading Engine v1.0
════════════════════════════════════════════════════════════════════
Current config: 5× leverage, wide ATR stops (backtest_leverage.py /
backtest_ratchet.py winner). 6.5yr backtest (Sep 2019 → Mar 2026):
$100 → $9,347 (+90.7%/yr), PF 1.60, WR 36.7%.

LOCKED PARAMETERS (do not change without re-backtesting):
  Signal:   RSI Divergence(14) + MACD Cross(12/26/9) + Volume(2×)
  Asset:    BTC/USDT isolated margin futures
  Frame:    15m candles
  Leverage: 5×
  Stops:    ATR × 8.0 (long) / ATR × 6.0 (short)
  Risk:     10% of corpus lost if the stop is hit (margin is separate)
  CB:       5 consecutive losses → 48h pause (flat)
  DCA:      $10/mo on 10th, +10%/yr
  Ratchet:  corpus UP after 10 net+ trades, DOWN after 10 consec losses

PAPER TRADE FIRST:
  PAPER_TRADE = true  (default in .env)  → simulates everything, no real orders
  Run 20+ paper trades, compare WR (~36.7%) and PF (~1.60) to backtest.
  Only set PAPER_TRADE=false in .env after confirming live performance.

USAGE:
  python bot.py               → paper trade (safe default)
  python bot.py --live        → live trade (requires API keys in .env)
  python bot.py --status      → print current state and exit
  python bot.py --reset       → wipe all state files and start fresh

API KEYS (for live mode only — set in .env, never hardcode):
  BINANCE_API_KEY=your_key
  BINANCE_API_SECRET=your_secret
════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import math
import logging
import argparse
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import ccxt
import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD as MACDIndicator
from ta.volatility import AverageTrueRange

from corpus_manager import CorpusManager

# ══════════════════════════════════════════════════════════════════════
#  CONFIGURATION — All locked parameters from 20 backtests
# ══════════════════════════════════════════════════════════════════════

# ── Required vars — checked explicitly so a missing one stops the bot
#    with a clear message instead of a raw TypeError from float(None).
_REQUIRED_ENV_VARS = [
    "START_BALANCE", "LEVERAGE", "RISK_PER_TRADE",
    "DCA_DAY", "DCA_MONTHLY_USD", "DCA_ANNUAL_GROWTH", "START_YEAR",
    "SYMBOL", "TIMEFRAME", "CANDLE_MINUTES", "LONG_ATR_MULT", "SHORT_ATR_MULT",
    "RSI_LEN", "MACD_FAST", "MACD_SLOW", "MACD_SIGNAL_WIN",
    "VOL_MULT", "VOL_SMA_PERIOD", "ATR_PERIOD",
    "DIV_WINDOW", "DIV_SHIFT", "DIV_MEMORY",
    "CB_TRIGGER", "CB_HOURS", "FEE_RATE", "CANDLES_NEEDED", "WARMUP",
]
_missing = [v for v in _REQUIRED_ENV_VARS if os.getenv(v) is None]
if _missing:
    raise RuntimeError(
        f"CRITICAL: Missing environment variable(s): {', '.join(_missing)}. "
        f"Copy env.example to .env and fill these in."
    )

# ── User-facing parameters — set these in .env ────────────────────
# PAPER_TRADE: "true" → simulate only. Set "false" in .env to go live.
PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() == "true"

# Starting balance for a fresh state (only used on first ever run)
START_BALANCE  = float(os.getenv("START_BALANCE"))

# Position sizing
LEVERAGE       = int(os.getenv("LEVERAGE"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE"))

# Hard affordability cap, NOT a strategy tunable: the largest fraction of
# free balance that may be locked as margin on one position. 1.0 = "never
# ask the exchange for more margin than the account holds", which is a
# constraint of reality rather than a parameter — that is why it defaults
# instead of joining _REQUIRED_ENV_VARS (an existing .env without it must
# still boot). Set it below 1.0 to keep dry powder.
MAX_MARGIN_FRAC = float(os.getenv("MAX_MARGIN_FRAC", "1.0"))

# ── Exit model ────────────────────────────────────────────────────
#   "stop"   — ATR stop-loss, position sized so the stop costs
#              RISK_PER_TRADE x corpus. The original model.
#   "nostop" — no stop at all. Size the MARGIN instead, and let
#              isolated-margin liquidation be the only forced exit, so
#              the margin posted IS the maximum loss. Validated
#              2026-08-24 (backtest_nostop.py + backtest_robustness.py):
#              PF 1.63, 25.5% max DD, 54.0% WR over 6.9 years, versus
#              PF 1.16 / 92.1% DD for "stop" — which additionally failed
#              three of four robustness gates.
#
# Defaults to "stop" ON PURPOSE. An existing .env has no EXIT_MODEL key,
# and a bot that silently changed how it exits positions on the next
# deploy would be the worst possible surprise. Adopting "nostop" is an
# explicit .env edit — same fail-safe posture as PAPER_TRADE.
EXIT_MODEL = os.getenv("EXIT_MODEL", "stop").strip().lower()
if EXIT_MODEL not in ("stop", "nostop"):
    raise RuntimeError(
        f"EXIT_MODEL must be 'stop' or 'nostop', got {EXIT_MODEL!r}"
    )
NOSTOP = EXIT_MODEL == "nostop"

# Maintenance margin rate — Binance BTCUSDT tier 1 (notional <= $50k).
# Only used to compute where liquidation sits; the exchange is the real
# authority and the bot never relies on this number to place an order.
MAINT_MARGIN_RATE = float(os.getenv("MAINT_MARGIN_RATE", "0.004"))

# Corpus / DCA
DCA_DAY        = int(os.getenv("DCA_DAY"))       # day of month for contribution
DCA_BASE       = float(os.getenv("DCA_MONTHLY_USD"))   # base monthly DCA ($)
DCA_GROWTH     = float(os.getenv("DCA_ANNUAL_GROWTH")) # 10% annual step-up
START_YEAR     = int(os.getenv("START_YEAR"))   # year the bot first ran

# ── LOCKED strategy parameters ───────────────────────────────────
# Defaults are the values fixed by the current validated backtest run
# (see backtest_leverage.py). Overridable via .env — but changing any
# value invalidates backtest results.
SYMBOL          = os.getenv("SYMBOL")
TIMEFRAME       = os.getenv("TIMEFRAME")
CANDLE_MINUTES  = int(os.getenv("CANDLE_MINUTES"))
LONG_ATR_MULT   = float(os.getenv("LONG_ATR_MULT"))
SHORT_ATR_MULT  = float(os.getenv("SHORT_ATR_MULT"))
RSI_LEN         = int(os.getenv("RSI_LEN"))
MACD_FAST       = int(os.getenv("MACD_FAST"))
MACD_SLOW       = int(os.getenv("MACD_SLOW"))
MACD_SIGNAL_WIN = int(os.getenv("MACD_SIGNAL_WIN"))
VOL_MULT        = float(os.getenv("VOL_MULT"))
VOL_SMA_PERIOD  = int(os.getenv("VOL_SMA_PERIOD"))
ATR_PERIOD      = int(os.getenv("ATR_PERIOD"))
DIV_WINDOW      = int(os.getenv("DIV_WINDOW"))
DIV_SHIFT       = int(os.getenv("DIV_SHIFT"))
DIV_MEMORY      = int(os.getenv("DIV_MEMORY"))
CB_TRIGGER      = int(os.getenv("CB_TRIGGER"))
CB_HOURS        = int(os.getenv("CB_HOURS"))
FEE_RATE        = float(os.getenv("FEE_RATE"))
CANDLES_NEEDED  = int(os.getenv("CANDLES_NEEDED"))
WARMUP          = int(os.getenv("WARMUP"))

# ── Files — all written to DATA_DIR (shared Docker volume) ────────
DATA_DIR         = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE        = os.path.join(DATA_DIR, "bot_state.json")
CORPUS_STATE_FILE = os.path.join(DATA_DIR, "corpus_state.json")
TRADE_LOG_FILE    = os.path.join(DATA_DIR, "trade_log.csv")
LOG_FILE          = os.path.join(DATA_DIR, "bot.log")
BOT_PAUSED_FILE   = os.path.join(DATA_DIR, "bot_paused.flag")

# ── Paper trade benchmarks ────────────────────────────────────────
# These gate the go-live decision: paper WR/PF must land within +/-20% of
# them. They MUST describe the model actually running, so they switch with
# EXIT_MODEL.
#
# nostop: backtest_nostop.py, BTC 15m 5x, 6.9yr — PF 1.63, WR 54.0%,
#   126 trades. Cleared all four gates in backtest_robustness.py
#   (in-sample PF 1.24, out-of-sample PF 1.97, bootstrap 5th pct 1.06,
#   best trade only 6.4% of gross profit) and an 18/18 parameter plateau.
#
# stop: backtest_leverage.py 5x on the CORRECTED engine — PF 1.16,
#   WR 27.1%, 177 trades. Recorded for comparison only. Do not go live on
#   it: it fails three of four robustness gates and its entire 6.9-year
#   net profit is a single trade (+$1,674 of +$1,540).
#
# The previous values (0.367 / 1.60) came from a pre-ruin-check run that
# traded through a -$547 balance and under-counted fees by a factor of
# leverage. They were never achievable.
if NOSTOP:
    BENCH_WR = 0.540
    BENCH_PF = 1.63
else:
    BENCH_WR = 0.271
    BENCH_PF = 1.16
BENCH_MIN_TRADES = 20


# ══════════════════════════════════════════════════════════════════════
#  LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════

def setup_logging():
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ]
    )

log = logging.getLogger("quantbot")


# ══════════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

FRESH_STATE = {
    "mode":               "paper",
    "start_date":         None,
    "start_balance":      START_BALANCE,
    "balance":            START_BALANCE,
    "position":           None,   # None or dict (see open_long/open_short)
    "bull_armed":         0,
    "bear_armed":         0,
    "consecutive_losses": 0,
    "cb_pause_until":     None,   # ISO timestamp string or None
    "total_trades":       0,
    "total_wins":         0,
    "total_pnl":          0.0,
    "total_fees":         0.0,
    "last_candle_ts":     None,
    "last_updated_at":    None,   # wall-clock UTC ISO timestamp — updated every candle
    "last_dca_month":     None,   # [year, month] list or None
}


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Ensure all keys exist (handles upgrades)
        for k, v in FRESH_STATE.items():
            data.setdefault(k, v)
        log.info(f"State loaded — balance ${data['balance']:.2f}  "
                 f"trades {data['total_trades']}")
        return data
    state = FRESH_STATE.copy()
    state["start_date"]    = datetime.now(timezone.utc).isoformat()
    state["start_balance"] = state["balance"]
    log.info(f"Fresh state — starting at ${state['balance']:.2f}")
    return state


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ══════════════════════════════════════════════════════════════════════
#  TRADE LOG  (CSV)
# ══════════════════════════════════════════════════════════════════════

TRADE_LOG_HEADER = (
    "datetime,side,entry_price,exit_price,stop_price,"
    "quantity_btc,pnl_usd,fees_usd,balance,reason,hold_candles,mode\n"
)


def append_trade_log(trade: dict):
    path   = Path(TRADE_LOG_FILE)
    header = not path.exists()
    with open(path, "a") as f:
        if header:
            f.write(TRADE_LOG_HEADER)
        f.write(",".join(str(v) for v in [
            trade["datetime"],
            trade["side"],
            round(trade["entry"], 2),
            round(trade["exit"], 2),
            round(trade["stop"], 2),
            round(trade["qty"], 8),
            round(trade["pnl"], 4),
            round(trade["fees"], 4),
            round(trade["balance"], 2),
            trade["reason"],
            trade["hold_candles"],
            trade["mode"],
        ]) + "\n")


# ══════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # RSI
    df["rsi"] = RSIIndicator(close=df["close"], window=RSI_LEN).rsi()

    # MACD
    macd              = MACDIndicator(
        close=df["close"],
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL_WIN,
    )
    df["macd_line"]   = macd.macd()
    df["signal_line"] = macd.macd_signal()

    # Volume spike
    df["avg_vol"]  = df["volume"].rolling(VOL_SMA_PERIOD).mean()
    df["high_vol"] = df["volume"] > (VOL_MULT * df["avg_vol"])

    # ATR
    df["atr"] = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=ATR_PERIOD
    ).average_true_range()

    # RSI divergence windows
    df["low_close"]  = df["close"].rolling(DIV_WINDOW).min()
    df["low_rsi"]    = df["rsi"].rolling(DIV_WINDOW).min()
    df["high_close"] = df["close"].rolling(DIV_WINDOW).max()
    df["high_rsi"]   = df["rsi"].rolling(DIV_WINDOW).max()

    # Bullish div: price makes lower low, RSI makes higher low
    df["bull_div"] = (
        (df["low_close"] < df["low_close"].shift(DIV_SHIFT)) &
        (df["low_rsi"]   > df["low_rsi"].shift(DIV_SHIFT))
    )

    # Bearish div: price makes higher high, RSI makes lower high
    df["bear_div"] = (
        (df["high_close"] > df["high_close"].shift(DIV_SHIFT)) &
        (df["high_rsi"]   < df["high_rsi"].shift(DIV_SHIFT))
    )

    # MACD crosses
    pm = df["macd_line"].shift(1)
    ps = df["signal_line"].shift(1)
    df["macd_bull_cross"] = (df["macd_line"] > df["signal_line"]) & (pm <= ps)
    df["macd_bear_cross"] = (df["macd_line"] < df["signal_line"]) & (pm >= ps)

    return df


# ══════════════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════

def cb_is_paused(state: dict) -> tuple:
    """Returns (paused: bool, hours_remaining: float)."""
    ts = state.get("cb_pause_until")
    if not ts:
        return False, 0.0
    resume = datetime.fromisoformat(ts)
    now    = datetime.now(timezone.utc)
    if now >= resume:
        state["cb_pause_until"] = None   # expired — clear it
        return False, 0.0
    remaining_h = (resume - now).total_seconds() / 3600
    return True, remaining_h


def cb_on_loss(state: dict) -> bool:
    """
    Call after every trade loss. Increments counter.
    Returns True if circuit breaker just triggered.
    """
    state["consecutive_losses"] += 1
    if state["consecutive_losses"] >= CB_TRIGGER:
        resume = datetime.now(timezone.utc) + timedelta(hours=CB_HOURS)
        state["cb_pause_until"]   = resume.isoformat()
        state["consecutive_losses"] = 0
        log.warning(
            f"🛑 CIRCUIT BREAKER — {CB_TRIGGER} consecutive losses  "
            f"→ pausing {CB_HOURS}h  "
            f"(resumes {resume.strftime('%Y-%m-%d %H:%M UTC')})"
        )
        return True
    return False


def manually_paused() -> bool:
    """True if /pause was sent via Telegram (notifier.py touches BOT_PAUSED_FILE)."""
    return Path(BOT_PAUSED_FILE).exists()


# ══════════════════════════════════════════════════════════════════════
#  EXCHANGE WRAPPER
# ══════════════════════════════════════════════════════════════════════

class Exchange:
    def __init__(self, paper: bool = True):
        self.paper = paper
        self._symbol = None

        api_key    = os.environ.get("BINANCE_API_KEY",    "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")

        params = {"enableRateLimit": True, "options": {"defaultType": "future"}}
        if not paper:
            params.update({"apiKey": api_key, "secret": api_secret})

        self._ex = ccxt.binanceusdm(params)

    def connect(self):
        log.info("Connecting to Binance...")
        self._ex.load_markets()
        for candidate in [SYMBOL, f"{SYMBOL}:USDT"]:
            if candidate in self._ex.symbols:
                self._symbol = candidate
                break
        if not self._symbol:
            raise RuntimeError(f"Symbol {SYMBOL} not found on Binance futures")
        log.info(f"Connected — {self._symbol}  ({'PAPER' if self.paper else 'LIVE'})")

    def configure_leverage(self):
        if self.paper:
            log.info(f"[paper] Leverage {LEVERAGE}× / isolated margin (simulated)")
            return
        try:
            self._ex.set_leverage(LEVERAGE, self._symbol)
            self._ex.set_margin_mode("isolated", self._symbol)
            log.info(f"Leverage {LEVERAGE}× set — isolated margin enabled")
            if NOSTOP:
                log.warning(
                    "EXIT_MODEL=nostop: the ONLY thing capping a losing trade "
                    "is isolated margin. Confirm on Binance that auto-add-"
                    "margin is OFF for this symbol — with it on, the exchange "
                    "tops the position up from the wallet and the loss cap "
                    "silently stops being a cap."
                )
        except Exception as e:
            # Some exchanges silently keep existing leverage — warn and continue
            log.warning(f"Could not set leverage: {e} — verify on Binance manually")

    def fetch_candles(self) -> pd.DataFrame:
        """Fetch recent closed candles as DataFrame."""
        raw = self._ex.fetch_ohlcv(
            self._symbol, TIMEFRAME, limit=CANDLES_NEEDED + 10
        )
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        df = df[~df.index.duplicated(keep="last")]
        return df.iloc[:-1]   # drop the still-open candle

    def funding_rate(self) -> float:
        """Current 8h funding rate as a fraction (0.0001 = 0.01%)."""
        return float(self._ex.fetch_funding_rate(self._symbol)["fundingRate"])

    def current_price(self) -> float:
        return float(self._ex.fetch_ticker(self._symbol)["last"])

    def market_limits(self) -> dict:
        """
        Real exchange constraints for the traded symbol.

        BTC/USDT perp: step 0.001 BTC, min qty 0.001 BTC, min notional $50.
        Nothing used to consult these — quantities went to ccxt raw, which
        truncates to the step (0.001813 -> 0.001, a 45% size error) or
        raises InvalidOrder below the minimum. Both are silent in paper
        mode, where no order is ever placed.
        """
        try:
            m = self._ex.market(self._symbol)
            return {
                "step":         float(m["precision"]["amount"] or 0.001),
                "min_qty":      float((m["limits"]["amount"] or {}).get("min") or 0.0),
                "min_notional": float((m["limits"]["cost"] or {}).get("min") or 0.0),
            }
        except Exception as e:
            log.warning(f"Could not read market limits ({e}) — using BTC perp defaults")
            return {"step": 0.001, "min_qty": 0.001, "min_notional": 50.0}

    def round_amount(self, qty: float) -> float:
        """Truncate qty to the exchange's lot step, exactly as ccxt would."""
        try:
            return float(self._ex.amount_to_precision(self._symbol, qty))
        except Exception:
            step = self.market_limits()["step"]
            return math.floor(qty / step) * step if step > 0 else qty

    def place_entry(self, side: str, qty: float, stop: float) -> dict:
        """
        Open a position.
        side:  'buy' for long, 'sell' for short
        Returns dict with filled_price and stop_order_id.
        """
        if self.paper:
            return {"filled_price": self.current_price(), "stop_order_id": "paper"}

        order = self._ex.create_market_order(
            self._symbol, side, qty, params={"reduceOnly": False}
        )
        time.sleep(0.3)
        filled = float(order.get("average") or order.get("price") or 0)

        if NOSTOP:
            # By design there is NO protective stop order. The loss ceiling
            # comes from isolated margin instead: the exchange can take the
            # margin posted and nothing more.
            #
            # This is strictly safer than the stop model's failure mode. In
            # "stop" mode, if the STOP_MARKET order fails to place and this
            # process then dies, the position is naked with an unbounded
            # loss. Here the ceiling holds even if the bot never runs again.
            #
            # It only holds while auto-add-margin stays OFF — that setting
            # would top the position up from the wallet and break the cap.
            log.info(
                f"No stop order placed (EXIT_MODEL=nostop). Loss is capped "
                f"at the posted margin by isolated margin; liquidation sits "
                f"~{100.0/LEVERAGE - MAINT_MARGIN_RATE*100:.1f}% away. "
                f"Verify auto-add-margin is DISABLED on Binance."
            )
            return {"filled_price": filled, "stop_order_id": None}

        # Entry is now live — retry the protective stop a few times rather
        # than leaving the position naked on one transient API failure.
        stop_side = "sell" if side == "buy" else "buy"
        stop_id   = None
        for attempt in range(1, 4):
            try:
                stop_order = self._ex.create_order(
                    self._symbol, "STOP_MARKET", stop_side, qty,
                    params={"stopPrice": stop, "reduceOnly": True}
                )
                stop_id = stop_order["id"]
                break
            except Exception as e:
                log.error(f"Stop-loss order attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    time.sleep(2)

        if stop_id is None:
            log.error(
                "🚨 CRITICAL: entry filled but the exchange stop-loss order could "
                "NOT be placed after 3 attempts — position has no hard stop on "
                "Binance. The bot's own per-candle stop check still protects it "
                "while this process keeps running. Verify manually on Binance."
            )

        return {"filled_price": filled, "stop_order_id": stop_id}

    def place_exit(self, pos_side: str, qty: float, stop_id: str) -> dict:
        """Close a position. pos_side: 'long' or 'short'."""
        if self.paper:
            return {"filled_price": self.current_price()}

        # Cancel stop-loss first
        if stop_id and stop_id != "paper":
            try:
                self._ex.cancel_order(stop_id, self._symbol)
            except Exception as e:
                log.warning(f"Could not cancel stop order {stop_id}: {e}")

        close_side = "sell" if pos_side == "long" else "buy"
        order = self._ex.create_market_order(
            self._symbol, close_side, qty, params={"reduceOnly": True}
        )
        filled = float(order.get("average") or order.get("price") or 0)
        return {"filled_price": filled}

    def get_exchange_position(self) -> dict | None:
        """Returns raw exchange position dict or None."""
        if self.paper:
            return None
        positions = self._ex.fetch_positions([self._symbol])
        for p in positions:
            if abs(float(p.get("contracts", 0) or 0)) > 0:
                return p
        return None


# ══════════════════════════════════════════════════════════════════════
#  POSITION SIZING
# ══════════════════════════════════════════════════════════════════════

def size_position(corpus: float, price: float,
                  stop_price: float) -> dict:
    """
    Stop-distance-aware position sizing.

    Guarantees that if the stop is hit, loss = RISK_PER_TRADE x corpus exactly.

    Formula:
      dollar_risk   = corpus x RISK_PER_TRADE
      stop_distance = abs(price - stop_price)
      qty           = dollar_risk / stop_distance
      notional      = qty x price
      margin        = notional / LEVERAGE      <- what the exchange locks

    P&L at stop = stop_distance x qty = dollar_risk   (always, any leverage)

    LEVERAGE DOES NOT APPEAR IN qty OR IN P&L. It only decides how much
    margin the exchange locks up and where the liquidation price sits.
    That is the whole point of stop-distance sizing: risk per trade is
    identical at 5x and at 20x, and only the margin footprint changes.

    Bug fixed 2026-08-24 — the phantom leverage multiplier:
      qty was  dollar_risk / (stop_distance x LEVERAGE)  and close_position
      paid out (exit-entry) x qty x LEVERAGE. The two cancelled, so the
      loss at stop was still right, but everything else was off by a
      factor of LEVERAGE: the stored qty was 1/LEVERAGE of the position
      that actually produces that P&L, the stored margin and the
      dashboard's "Invested $" were 1/LEVERAGE of the real margin (a $140
      position showed as $7 and then "lost $15"), and LIVE mode sent that
      1/LEVERAGE quantity to Binance — where no leverage multiplier
      exists — so real fills would have returned 1/LEVERAGE of the P&L
      the bot booked into its own balance. Equity curves are unchanged by
      the fix; qty, notional and margin are now the real numbers.

    Older bug, kept for the record: qty = (corpus x RISK x LEVERAGE)/price
      with the same x LEVERAGE payout squared the leverage. A 0.47% ATR
      gave an 18.8% loss instead of 10%; a 2.5% ATR would have wiped the
      account in one trade.

    Example: corpus=$110, price=$70121, stop=$70448 (ATR=$218), LEVERAGE=5
      dollar_risk   = $11.00
      stop_distance = $327
      qty           = 11 / 327          = 0.033639 BTC
      notional      = 0.033639 x 70121  = $2,358.87
      margin        = 2358.87 / 5       = $471.77   <- locked by Binance
      P&L at stop   = 327 x 0.033639    = $11.00 = 10% of corpus
      liquidation   = ~20% away (100/5), stop is 0.47% away
    """
    dollar_risk   = corpus * RISK_PER_TRADE
    stop_distance = abs(price - stop_price)
    # Guard: never let stop be too close (< 0.01% of price)
    min_distance  = price * 0.0001
    stop_distance = max(stop_distance, min_distance)
    qty      = dollar_risk / stop_distance
    notional = qty * price
    margin   = notional / LEVERAGE
    return {"margin": margin, "notional": notional, "qty": qty,
            "stop_distance": stop_distance, "dollar_risk": dollar_risk}


def liq_price(side: str, entry: float, leverage: int = None) -> float:
    """
    Isolated-margin liquidation price, one-way mode.

        long  : entry x (1 - 1/lev + MMR)
        short : entry x (1 + 1/lev - MMR)

    At 5x that is ~19.6% away; at 20x, ~4.6%. This is the standard
    approximation and it is used for MONITORING ONLY — the exchange
    computes the real liquidation price and is the only authority. The bot
    never places an order at this level.

    Note the direction of the approximation error: Binance charges funding
    against an isolated position's own margin, which walks the real
    liquidation price CLOSER as a position ages. So the true liquidation
    is hit slightly sooner than this returns, never later.
    """
    lev = leverage or LEVERAGE
    imr = 1.0 / lev
    if side == "long":
        return entry * (1 - imr + MAINT_MARGIN_RATE)
    return entry * (1 + imr - MAINT_MARGIN_RATE)


def size_position_nostop(corpus: float, price: float) -> dict:
    """
    Margin-first sizing for the no-stop model.

        margin   = corpus x RISK_PER_TRADE     <- IS the maximum loss
        notional = margin x LEVERAGE
        qty      = notional / price

    With no stop there is no stop distance to size against, so the margin
    becomes the risk decision directly. Isolated margin cannot lose more
    than the margin posted, so "risk per trade" stops being a target that
    slippage can overshoot and becomes a hard ceiling enforced by the
    exchange.

    Contrast with size_position(): there, margin is a CONSEQUENCE of the
    stop width — margin/corpus = RISK/(stop_pct x LEVERAGE), which works
    out to ~1x corpus for a 2% stop at 5x and 1.6x corpus for a 0.31% stop
    at 20x. Here margin is 10% of corpus, full stop. That ~8x smaller
    footprint is why fees fall from $581 to $64 over the 6.9-year backtest.

    Example: corpus=$100, price=$77000, LEVERAGE=5
      margin   = $10.00          <- most this trade can ever lose
      notional = $50.00
      qty      = 0.000649 BTC
      liq      = 19.6% away
    """
    margin   = corpus * RISK_PER_TRADE
    notional = margin * LEVERAGE
    qty      = notional / price
    return {"margin": margin, "notional": notional, "qty": qty,
            "stop_distance": 0.0, "dollar_risk": margin}


def fit_to_margin(sizing: dict, free_balance: float) -> dict:
    """
    Shrink a position until the margin it needs fits the balance we have.

    Nothing used to check this. Under stop-distance sizing the margin a
    trade needs is

        margin / corpus = RISK_PER_TRADE / (stop_pct x LEVERAGE)

    which at 10% risk is ~1.0x corpus for a 2% stop at 5x, and 1.6x corpus
    for a 0.31% stop at 20x. Corpus only ratchets after 10 trades, so it
    drifts above balance during a drawdown and the bot then sizes trades
    it cannot fund. A real exchange rejects those outright; the paper
    engine happily filled them and booked the loss.

    Policy: scale down to fit rather than skip the signal — a trader with
    $50 free takes the trade smaller, they do not sit it out. Risk for
    that trade is reduced by the same ratio, which is reported in
    `scaled_to`.
    """
    cap = free_balance * MAX_MARGIN_FRAC
    if cap <= 0 or sizing["margin"] <= cap:
        return {**sizing, "scaled_to": 1.0}
    # Scaling is proportional in both models: qty, notional, margin and the
    # risk figure all move by the same ratio, so the "max loss = margin"
    # identity of the no-stop model survives being shrunk to fit.
    ratio = cap / sizing["margin"]
    return {
        **sizing,
        "qty":         sizing["qty"] * ratio,
        "notional":    sizing["notional"] * ratio,
        "margin":      cap,
        "dollar_risk": sizing["dollar_risk"] * ratio,
        "scaled_to":   ratio,
    }



# ══════════════════════════════════════════════════════════════════════
#  CORE BOT
# ══════════════════════════════════════════════════════════════════════

class QuantBot:

    def __init__(self, ex: Exchange, state: dict, corpus_mgr: CorpusManager):
        self.ex  = ex
        self.st  = state
        self.cm  = corpus_mgr
        self._entry_candle_n = 0    # for measuring hold time
        self._corpus_this_candle = corpus_mgr.corpus

    # ─── DCA ──────────────────────────────────────────────────────────
    def check_dca(self, now: datetime):
        if now.day != DCA_DAY:
            return
        key = [now.year, now.month]
        if self.st.get("last_dca_month") == key:
            return
        result = self.cm.on_monthly_refresh(
            self.st["balance"], now.year, now.month, START_YEAR
        )
        contrib = result["contribution"]
        if contrib > 0:
            self.st["balance"]        += contrib
            self.st["last_dca_month"]  = key
            save_state(self.st)
            self.cm.save_state()
            log.info(
                f"DCA +${contrib:.2f} → balance ${self.st['balance']:.2f}  "
                f"| corpus ${self.cm.corpus:.2f}"
            )

    # ─── LIVE RECONCILIATION ──────────────────────────────────────────
    def exchange_position_gone(self) -> bool:
        """
        True when state says we hold a position but the exchange does not.

        In the no-stop model liquidation is the primary loss event and it
        happens entirely exchange-side — nothing tells the bot. Without a
        per-candle check, `bot_state.json` would keep a phantom position
        open indefinitely: the bot would later "close" something that does
        not exist, fire a reduceOnly order that no-ops, and log a P&L that
        never happened.

        Reconciliation used to run only at startup. That was already the
        highest-value open bug for the stop model; for this one it is not
        optional. Any API failure returns False — a transient error must
        never be read as "your position vanished".
        """
        if self.ex.paper or self.st.get("position") is None:
            return False
        try:
            return self.ex.get_exchange_position() is None
        except Exception as e:
            log.warning(f"Could not reconcile against exchange: {e}")
            return False

    # ─── FUNDING ──────────────────────────────────────────────────────
    def charge_funding(self, candle: pd.Series):
        """
        Charge perpetual funding on an open position, in PAPER mode only.

        The stop model holds for a few candles, so funding rounds to
        nothing and the rest of this repo ignores it. The no-stop model
        holds for ~18 days on average and up to 122, over which 8h funding
        on a levered notional becomes a first-order cost. Leaving it out
        would make paper results beat the backtest for a reason that has
        nothing to do with the signal — and paper-vs-benchmark is exactly
        what gates the go-live decision.

        Live mode does NOT call this: Binance debits funding from the
        position's margin directly, so charging it here as well would
        double-count it.

        Binance settles at 00:00 / 08:00 / 16:00 UTC. A 15m candle stamped
        on one of those hours is the one containing that settlement.
        """
        pos = self.st.get("position")
        if pos is None or not self.ex.paper:
            return
        ts = candle.name
        if not (hasattr(ts, "hour") and ts.hour in (0, 8, 16) and ts.minute == 0):
            return
        try:
            rate = float(self.ex.funding_rate())
        except Exception as e:
            log.debug(f"Funding rate unavailable ({e}) — skipping this settlement")
            return
        notional = pos["quantity"] * float(candle["close"])
        cost     = rate * notional
        if pos["side"] == "short":
            cost = -cost                    # shorts receive when rate > 0
        self.st["balance"]   -= cost
        pos["funding_paid"]   = pos.get("funding_paid", 0.0) + cost
        save_state(self.st)
        log.info(f"[paper] funding {rate*100:+.4f}% on ${notional:,.2f} "
                 f"= ${-cost:+.4f}  balance=${self.st['balance']:,.2f}")

    # ─── SIZE / VALIDATE AN ENTRY ─────────────────────────────────────
    def _size_or_skip(self, price: float, stop: float, side: str) -> dict | None:
        """
        Turn a signal into an order the exchange will actually accept, or
        None if it cannot be placed.

        Three gates, in order:
          1. stop-distance sizing        -> risk exactly RISK_PER_TRADE x corpus
          2. margin affordability        -> never lock more than we hold
          3. exchange lot step / minimums-> the qty Binance will really fill

        Gate 3 is the one that used to be missing entirely: the raw qty went
        straight to ccxt, which truncates to the 0.001 BTC step or raises
        below it. Rounding here (instead of letting ccxt do it silently)
        means the qty we log, size and book P&L against is the qty that
        actually trades.
        """
        corpus = getattr(self, "_corpus_this_candle", self.cm.corpus)
        base   = (size_position_nostop(corpus, price) if NOSTOP
                  else size_position(corpus, price, stop))
        sizing = fit_to_margin(base, self.st["balance"])
        lim    = self.ex.market_limits()
        qty    = self.ex.round_amount(sizing["qty"])

        if qty <= 0 or qty < lim["min_qty"]:
            why = (f"Corpus ${corpus:,.2f} x {RISK_PER_TRADE:.0%} margin "
                   f"x {LEVERAGE}x is only ${sizing['notional']:,.2f} of notional"
                   if NOSTOP else
                   f"Corpus ${corpus:,.2f} x {RISK_PER_TRADE:.0%} risk is "
                   f"too small for a {abs(price-stop)/price:.2%} stop at this price")
            log.warning(
                f"SKIP {side} — sized {sizing['qty']:.6f} BTC, below the "
                f"exchange minimum {lim['min_qty']:.6f}. {why}."
            )
            return None

        notional = qty * price
        if lim["min_notional"] and notional < lim["min_notional"]:
            log.warning(
                f"SKIP {side} — notional ${notional:,.2f} below the exchange "
                f"minimum ${lim['min_notional']:,.2f}."
            )
            return None

        margin = notional / LEVERAGE
        if margin > self.st["balance"]:
            log.warning(
                f"SKIP {side} — margin ${margin:,.2f} exceeds balance "
                f"${self.st['balance']:,.2f} even after rounding down."
            )
            return None

        if sizing["scaled_to"] < 1.0:
            log.warning(
                f"{side.upper()} scaled to {sizing['scaled_to']:.0%} of target "
                f"size — full size needed ${sizing['margin']/sizing['scaled_to']:,.2f} "
                f"margin against a ${self.st['balance']:,.2f} balance. "
                f"Risk this trade: ${sizing['dollar_risk']:,.2f}."
            )

        return {**sizing, "qty": qty, "notional": notional, "margin": margin}

    # ─── OPEN LONG ────────────────────────────────────────────────────
    def open_long(self, candle: pd.Series, candle_n: int):
        price    = float(candle["close"])
        atr      = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0
        if NOSTOP:
            # No stop-loss. `stop` carries the LIQUIDATION price instead —
            # the level at which this trade dies — so state, logs and the
            # trade log keep one consistent "where does this end" field.
            stop = liq_price("long", price)
        else:
            stop = price - (LONG_ATR_MULT * atr) if atr > 0 else price * 0.95
        sizing   = self._size_or_skip(price, stop, "long")
        if sizing is None:
            return
        qty      = sizing["qty"]
        fee_in   = price * qty * FEE_RATE

        result   = self.ex.place_entry("buy", qty, stop)
        filled   = result["filled_price"] or price
        stop_id  = result["stop_order_id"]

        self.st["balance"]     -= fee_in
        self.st["total_fees"]  += fee_in
        self.st["bull_armed"]   = 0
        self.st["position"]     = {
            "side":          "long",
            "entry_price":   filled,
            "stop_price":    stop,
            "quantity":      qty,
            "margin":        sizing["margin"],
            "entry_fee":     fee_in,
            "entry_time":    datetime.now(timezone.utc).isoformat(),
            "stop_order_id": stop_id,
            "exit_model":    EXIT_MODEL,
            "funding_paid":  0.0,
        }
        self._entry_candle_n = candle_n
        save_state(self.st)

        tag = "[paper] " if self.ex.paper else ""
        log.info(
            f"{tag}LONG OPEN  price=${filled:,.2f}  "
            f"{'liq' if NOSTOP else 'stop'}=${stop:,.2f} "
            f"({abs(stop-filled)/filled:.2%} away)  qty={qty:.6f} BTC  "
            f"notional=${sizing['notional']:,.2f}  "
            f"margin=${sizing['margin']:,.2f} ({LEVERAGE}x)  "
            f"max_loss=${sizing['margin' if NOSTOP else 'dollar_risk']:,.2f}  "
            f"corpus=${self.cm.corpus:.2f}"
        )

    # ─── OPEN SHORT ───────────────────────────────────────────────────
    def open_short(self, candle: pd.Series, candle_n: int):
        price    = float(candle["close"])
        atr      = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0
        if NOSTOP:
            # No stop-loss. `stop` carries the LIQUIDATION price instead —
            # the level at which this trade dies — so state, logs and the
            # trade log keep one consistent "where does this end" field.
            stop = liq_price("short", price)
        else:
            stop = price + (SHORT_ATR_MULT * atr) if atr > 0 else price * 1.05
        sizing   = self._size_or_skip(price, stop, "short")
        if sizing is None:
            return
        qty      = sizing["qty"]
        fee_in   = price * qty * FEE_RATE

        result   = self.ex.place_entry("sell", qty, stop)
        filled   = result["filled_price"] or price
        stop_id  = result["stop_order_id"]

        self.st["balance"]     -= fee_in
        self.st["total_fees"]  += fee_in
        self.st["bear_armed"]   = 0
        self.st["position"]     = {
            "side":          "short",
            "entry_price":   filled,
            "stop_price":    stop,
            "quantity":      qty,
            "margin":        sizing["margin"],
            "entry_fee":     fee_in,
            "entry_time":    datetime.now(timezone.utc).isoformat(),
            "stop_order_id": stop_id,
            "exit_model":    EXIT_MODEL,
            "funding_paid":  0.0,
        }
        self._entry_candle_n = candle_n
        save_state(self.st)

        tag = "[paper] " if self.ex.paper else ""
        log.info(
            f"{tag}SHORT OPEN  price=${filled:,.2f}  "
            f"{'liq' if NOSTOP else 'stop'}=${stop:,.2f} "
            f"({abs(stop-filled)/filled:.2%} away)  qty={qty:.6f} BTC  "
            f"notional=${sizing['notional']:,.2f}  "
            f"margin=${sizing['margin']:,.2f} ({LEVERAGE}x)  "
            f"max_loss=${sizing['margin' if NOSTOP else 'dollar_risk']:,.2f}  "
            f"corpus=${self.cm.corpus:.2f}"
        )

    # ─── CLOSE POSITION ───────────────────────────────────────────────
    def close_position(self, candle: pd.Series, reason: str, candle_n: int):
        pos = self.st.get("position")
        if pos is None:
            return

        side    = pos["side"]
        entry   = pos["entry_price"]
        stop    = pos["stop_price"]
        qty     = pos["quantity"]
        fee_in  = pos["entry_fee"]

        margin   = pos.get("margin", 0.0)
        funding  = pos.get("funding_paid", 0.0)
        liquidated = reason == "liquidated"

        # Exit price: the level that ended the trade, else the signal
        # candle's close.
        if reason in ("stop", "liquidated"):
            exit_price = stop
        else:
            exit_price = float(candle["close"])

        if not self.ex.paper and not liquidated:
            result     = self.ex.place_exit(side, qty, pos.get("stop_order_id"))
            exit_price = result["filled_price"] or exit_price
        elif not self.ex.paper and liquidated:
            # The exchange already closed it. Do NOT send a reduceOnly order
            # for a position that no longer exists — just cancel any residue.
            log.critical(
                f"LIQUIDATED on the exchange — {side.upper()} @ ${entry:,.2f}, "
                f"margin ${margin:,.2f} lost. Position closed by Binance, not "
                f"by the bot. Verify the account before it trades again."
            )

        # P&L (mirrors backtest formula exactly)
        fee_out = exit_price * qty * FEE_RATE
        # Real futures P&L — leverage is NOT a multiplier here, it only
        # set how much margin was locked and where liquidation sat.
        if liquidated:
            # Isolated margin: the exchange takes the margin posted and
            # nothing more. Funding already left the balance during the
            # hold, so add it back here to keep the TOTAL loss equal to
            # margin + the entry fee rather than double-charging it.
            raw_pnl = -(margin - funding)
            fee_out = 0.0                # the liquidation fee is inside the margin
        elif side == "long":
            raw_pnl = (exit_price - entry) * qty
        else:
            raw_pnl = (entry - exit_price) * qty
        pnl = raw_pnl - fee_in - fee_out - funding

        # fee_in already left the balance when the position opened, so only
        # fee_out is new here. Adding the full `pnl` charged the entry fee a
        # second time (~0.05% of notional per trade) and meant
        # balance != start_balance + total_pnl + dca could never reconcile.
        # Fixed 2026-08-24, together with the same bug in the backtests.
        self.st["balance"]      += raw_pnl - fee_out
        self.st["total_fees"]   += fee_out
        self.st["total_trades"] += 1
        self.st["total_pnl"]    += pnl
        hold = candle_n - self._entry_candle_n

        if pnl > 0:
            self.st["total_wins"]       += 1
            self.st["consecutive_losses"] = 0
            result_tag = f"WIN  +${pnl:,.2f}"
        else:
            result_tag = f"LOSS  ${pnl:,.2f}"
            cb_on_loss(self.st)

        # Exactly ONE ratchet call per trade, unconditionally.
        #
        # This used to be two calls guarded by
        #     if cb_fired: on_trade_complete(...)
        #     if pnl > 0 or not (pnl <= 0 and cb_pause_until): on_trade_complete(...)
        # The guard was meant to avoid double-counting when the circuit
        # breaker had just fired, but `cb_pause_until` is set by ANY pause
        # still running, not only a fresh trigger. So a losing trade that
        # closed while an older CB pause was active (entirely possible — the
        # CB blocks entries, not exits) reached CorpusManager zero times, and
        # its trade_count / consecutive_losses / net_since_ratchet silently
        # desynced from bot_state.json.
        #
        # The backtests call on_trade_complete unconditionally once per
        # trade, so this was a real live-vs-backtest divergence in ratchet
        # timing. Fixed 2026-08-24 to match them.
        corpus_ev = self.cm.on_trade_complete(pnl, self.st["balance"])
        act = corpus_ev.get("action", "")
        if "ratchet" in act:
            log.info(f"Corpus ratchet: {act}")

        tag = "[paper] " if self.ex.paper else ""
        log.info(
            f"{tag}{side.upper()} CLOSE  {result_tag}  "
            f"exit=${exit_price:,.2f}  reason={reason}  "
            f"hold={hold}c  balance=${self.st['balance']:.2f}"
        )

        append_trade_log({
            "datetime":    datetime.now(timezone.utc).isoformat(),
            "side":        side,
            "entry":       entry,
            "exit":        exit_price,
            "stop":        stop,
            "qty":         qty,
            "pnl":         pnl,
            "fees":        fee_in + fee_out,
            "balance":     self.st["balance"],
            "reason":      reason,
            "hold_candles": hold,
            "mode":        "paper" if self.ex.paper else "live",
        })

        self.st["position"] = None
        save_state(self.st)
        self.cm.save_state()

        if self.ex.paper and self.st["total_trades"] >= BENCH_MIN_TRADES:
            if self.st["total_trades"] % 5 == 0:
                self._benchmark_check()

    # ─── PROCESS CANDLE ───────────────────────────────────────────────
    def process(self, df: pd.DataFrame, candle_n: int):
        """
        Main logic per closed candle.
        Order: DCA → update armed → check exits → check entries.
        """
        now    = datetime.now(timezone.utc)
        candle = df.iloc[-1]
        price  = float(candle["close"])
        atr    = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0

        # 1. DCA
        self.check_dca(now)

        # Position size for THIS candle is decided from the corpus as of the
        # candle open, before any exit on this bar can ratchet it.
        #
        # This is not an accident of ordering — an exit and a fresh entry can
        # legitimately happen on the same candle (§4.5), and reading
        # cm.corpus at entry time would let the exit's ratchet immediately
        # inflate the entry that follows it on the same bar. The backtests
        # snapshot the corpus at the top of the bar, so doing anything else
        # here makes live results stop matching the benchmark that gates
        # go-live. Verified by replay: without this the engines drift to
        # PF 1.59 vs the backtest's 1.63 over 6.9 years.
        self._corpus_this_candle = self.cm.corpus

        # 2. Update armed signal counters
        if candle.get("bull_div", False):
            self.st["bull_armed"] = DIV_MEMORY
        elif self.st["bull_armed"] > 0:
            self.st["bull_armed"] -= 1

        if candle.get("bear_div", False):
            self.st["bear_armed"] = DIV_MEMORY
        elif self.st["bear_armed"] > 0:
            self.st["bear_armed"] -= 1

        bull = self.st["bull_armed"]
        bear = self.st["bear_armed"]
        pos  = self.st.get("position")

        # 2b. Funding (no-stop holds run into weeks — see charge_funding)
        if NOSTOP:
            self.charge_funding(candle)

        # 3. Exits
        if pos is not None:
            side  = pos["side"]
            entry = pos["entry_price"]
            stop  = pos["stop_price"]

            if NOSTOP:
                # Liquidation first, and checked INTRABAR against the
                # candle's high/low. Every other exit in this bot triggers
                # on the close, but a liquidation does not wait for the
                # close — the exchange fires the moment the wick touches.
                # Checking the close here would silently hide most
                # liquidations behind wicks and make paper results look far
                # better than the account would really have done.
                hit_liq = (float(candle["low"]) <= stop if side == "long"
                           else float(candle["high"]) >= stop)
                if not self.ex.paper:
                    # The exchange is the authority, not our arithmetic.
                    # If it already closed us out, believe it.
                    hit_liq = hit_liq or self.exchange_position_gone()
                if hit_liq:
                    self.close_position(candle, "liquidated", candle_n)
                else:
                    opposite = ((bear > 0 and bool(candle.get("macd_bear_cross")))
                                if side == "long" else
                                (bull > 0 and bool(candle.get("macd_bull_cross"))))
                    if opposite and bool(candle.get("high_vol")):
                        self.close_position(candle, "signal", candle_n)

            elif side == "long":
                signal_exit = (bear > 0 and
                               bool(candle.get("macd_bear_cross")) and
                               bool(candle.get("high_vol")))
                stop_exit   = atr > 0 and price <= stop
                if signal_exit:
                    self.close_position(candle, "signal", candle_n)
                elif stop_exit:
                    self.close_position(candle, "stop", candle_n)

            elif side == "short":
                signal_exit = (bull > 0 and
                               bool(candle.get("macd_bull_cross")) and
                               bool(candle.get("high_vol")))
                stop_exit   = atr > 0 and price >= stop
                if signal_exit:
                    self.close_position(candle, "signal", candle_n)
                elif stop_exit:
                    self.close_position(candle, "stop", candle_n)

        # 4. Entries (only if flat)
        if self.st.get("position") is None:
            paused, hours_left = cb_is_paused(self.st)
            if paused:
                log.debug(f"CB pause active — {hours_left:.1f}h remaining")
            elif manually_paused():
                log.debug("Manual pause active (/pause via Telegram) — skipping entries")
            else:
                # Long entry
                if (bull > 0 and
                        bool(candle.get("macd_bull_cross")) and
                        bool(candle.get("high_vol"))):
                    self.open_long(candle, candle_n)

                # Short entry
                elif (bear > 0 and
                      bool(candle.get("macd_bear_cross")) and
                      bool(candle.get("high_vol"))):
                    self.open_short(candle, candle_n)

    # ─── DASHBOARD ────────────────────────────────────────────────────
    def dashboard(self, df: pd.DataFrame):
        price     = float(df.iloc[-1]["close"])
        n         = self.st["total_trades"]
        wins      = self.st["total_wins"]
        wr        = wins / n * 100 if n > 0 else 0.0
        net_pnl   = self.st.get("total_pnl", 0.0)
        start_bal = self.st.get("start_balance", self.st["balance"])
        ret_pct   = (self.st["balance"] - start_bal) / start_bal * 100
        mode_tag  = "[PAPER]" if self.ex.paper else "[LIVE]"
        paused, hrs = cb_is_paused(self.st)
        cb_tag    = f"  🛑 CB paused {hrs:.1f}h" if paused else ""

        sep = "═" * 58
        print(f"\n{sep}")
        print(f"  QuantBot {mode_tag}  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{sep}")
        print(f"  BTC price:   ${price:>12,.2f}")
        print(f"  Balance:     ${self.st['balance']:>12,.2f}  ({ret_pct:+.1f}%)")
        print(f"  Corpus:      ${self.cm.corpus:>12,.2f}")
        print(f"  Trades:      {n:>4}  wins {wins}  WR {wr:.1f}%  net P&L ${net_pnl:+,.2f}")
        print(f"  Armed:       bull={self.st['bull_armed']}  "
              f"bear={self.st['bear_armed']}  "
              f"consec_loss={self.st['consecutive_losses']}{cb_tag}")

        pos = self.st.get("position")
        if pos:
            side  = pos["side"]
            entry = pos["entry_price"]
            stop  = pos["stop_price"]
            if side == "long":
                unreal = (price - entry) * pos["quantity"]
            else:
                unreal = (entry - price) * pos["quantity"]
            entry_fee = entry * pos["quantity"] * FEE_RATE
            unreal_net = unreal - entry_fee
            print(f"  Position:    {side.upper()}  entry=${entry:,.2f}  "
                  f"{'liq' if NOSTOP else 'stop'}=${stop:,.2f}  "
                  f"unreal=${unreal_net:+,.2f}")
        else:
            print(f"  Position:    NONE")
        print(sep)

    # ─── PAPER BENCHMARK CHECK ────────────────────────────────────────
    def _benchmark_check(self):
        n    = self.st["total_trades"]
        wins = self.st["total_wins"]
        wr   = wins / n if n > 0 else 0
        pf   = None

        if Path(TRADE_LOG_FILE).exists():
            tl  = pd.read_csv(TRADE_LOG_FILE)
            w   = tl[tl["pnl_usd"] > 0]["pnl_usd"].sum()
            l   = abs(tl[tl["pnl_usd"] <= 0]["pnl_usd"].sum())
            pf  = w / l if l > 0 else None

        dwr = (wr - BENCH_WR) / BENCH_WR * 100
        print(f"\n{'━'*58}")
        print(f"  📊 PAPER PERFORMANCE  ({n} trades)")
        print(f"{'─'*58}")
        print(f"  Win Rate:        {wr*100:.1f}%  (backtest {BENCH_WR*100:.1f}%  Δ{dwr:+.0f}%)")
        if pf:
            dpf = (pf - BENCH_PF) / BENCH_PF * 100
            print(f"  Profit Factor:   {pf:.2f}   (backtest {BENCH_PF:.2f}  Δ{dpf:+.0f}%)")
        print(f"  Net P&L:         ${self.st.get('total_pnl', 0):+,.2f}")
        print(f"  Balance:         ${self.st['balance']:,.2f}")
        within_20 = abs(wr - BENCH_WR) / BENCH_WR <= 0.20
        if within_20:
            print(f"\n  ✅ Within 20% of backtest benchmarks.")
            print(f"     To go live: set PAPER_TRADE=false in .env")
        else:
            print(f"\n  ⚠️  Outside 20% benchmark threshold. Keep paper trading.")
        print(f"{'━'*58}\n")


# ══════════════════════════════════════════════════════════════════════
#  TIMING
# ══════════════════════════════════════════════════════════════════════

def secs_to_next_candle() -> float:
    now      = datetime.now(timezone.utc)
    elapsed  = (now.minute % CANDLE_MINUTES) * 60 + now.second
    return (CANDLE_MINUTES * 60 - elapsed) + 6   # +6s buffer after close


def wait_for_candle():
    secs = secs_to_next_candle()
    if secs > 15:
        m, s = divmod(int(secs), 60)
        log.info(f"Next candle in {m}m {s}s ...")
    time.sleep(secs)


# ══════════════════════════════════════════════════════════════════════
#  STATUS COMMAND
# ══════════════════════════════════════════════════════════════════════

def cmd_status():
    if not Path(STATE_FILE).exists():
        print("No state file — bot has not been run yet.")
        return
    st  = load_state()
    n   = st["total_trades"]
    wr  = st["total_wins"] / n * 100 if n > 0 else 0
    s_b = st.get("start_balance", st["balance"])
    ret = (st["balance"] - s_b) / s_b * 100

    print(f"\n{'═'*58}")
    print(f"  QuantBot Status  ({'PAPER' if st['mode']=='paper' else 'LIVE'})")
    print(f"{'═'*58}")
    print(f"  Started:      {str(st.get('start_date','?'))[:10]}")
    print(f"  Start bal:    ${s_b:.2f}")
    print(f"  Balance:      ${st['balance']:.2f}  ({ret:+.1f}%)")
    print(f"  Trades:       {n}  WR: {wr:.1f}%  Net: ${st.get('total_pnl',0):+.2f}")
    print(f"  Consec loss:  {st['consecutive_losses']}")
    print(f"  CB pause:     {st.get('cb_pause_until') or 'none'}")
    pos = st.get("position")
    if pos:
        lvl = "liq" if pos.get("exit_model") == "nostop" else "stop"
        print(f"  Open pos:     {pos['side'].upper()} @ ${pos['entry_price']:,.2f}  "
              f"{lvl} ${pos['stop_price']:,.2f}"
              + (f"  margin ${pos['margin']:,.2f}" if pos.get("margin") else ""))
    else:
        print(f"  Open pos:     none")

    if Path(CORPUS_STATE_FILE).exists():
        with open(CORPUS_STATE_FILE) as f:
            cs = json.load(f)
        print(f"  Corpus:       ${cs['corpus']:.2f}")
        print(f"  Total DCA:    ${cs['total_dca_added']:.2f}")

    if Path(TRADE_LOG_FILE).exists():
        tl = pd.read_csv(TRADE_LOG_FILE)
        if len(tl) > 0:
            print(f"\n  Last trades:")
            for _, row in tl.tail(5).iterrows():
                pnl_tag = f"+${row['pnl_usd']:.2f}" if row['pnl_usd'] > 0 else f"${row['pnl_usd']:.2f}"
                print(f"    {str(row['datetime'])[:10]}  "
                      f"{row['side']:<5}  "
                      f"${row['entry_price']:>8,.0f}→${row['exit_price']:>8,.0f}  "
                      f"{pnl_tag:>10}  bal ${row['balance']:,.2f}")
    print()


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main(go_live: bool = False):
    setup_logging()
    paper = not go_live

    # Safety checks
    if go_live and PAPER_TRADE:
        log.error("PAPER_TRADE=true in .env but --live flag passed. "
                  "Set PAPER_TRADE=false in .env first.")
        sys.exit(1)
    if go_live and not os.environ.get("BINANCE_API_KEY"):
        log.error("BINANCE_API_KEY not set. Run: export BINANCE_API_KEY=...")
        sys.exit(1)

    log.info("━" * 58)
    log.info(f"  QuantBot  {'PAPER' if paper else 'LIVE'}  "
             f"{SYMBOL} {TIMEFRAME} {LEVERAGE}×  {RISK_PER_TRADE*100:.0f}% risk")
    if NOSTOP:
        log.info(f"  EXIT: no stop-loss — margin IS the max loss "
                 f"({RISK_PER_TRADE*100:.0f}% of corpus), liquidation "
                 f"~{100.0/LEVERAGE - MAINT_MARGIN_RATE*100:.1f}% away")
    else:
        log.info(f"  EXIT: ATR stop ×{LONG_ATR_MULT}L/×{SHORT_ATR_MULT}S  "
                 f"(liquidation {100.0/LEVERAGE:.0f}% away)")
    log.info(f"  Benchmarks: WR {BENCH_WR:.1%}  PF {BENCH_PF:.2f}  "
             f"(EXIT_MODEL={EXIT_MODEL})")
    log.info(f"  CB: {CB_TRIGGER} losses → {CB_HOURS}h  |  "
             f"DCA: ${DCA_BASE}/mo on {DCA_DAY}th")
    log.info("━" * 58)

    state = load_state()
    state["mode"] = "paper" if paper else "live"
    save_state(state)

    # Connect
    ex = Exchange(paper=paper)
    ex.connect()
    ex.configure_leverage()

    # Corpus manager
    cm = CorpusManager(
        initial_balance    = state["balance"],
        base_monthly_dca   = DCA_BASE,
        dca_annual_growth  = DCA_GROWTH,
        ratchet_up_every   = 10,
        ratchet_down_after = 10,
    )
    if Path(CORPUS_STATE_FILE).exists():
        cm.load_state()
    else:
        cm.corpus = state["balance"]

    # Sync with exchange on startup
    if not paper:
        exch_pos  = ex.get_exchange_position()
        state_pos = state.get("position")
        if exch_pos and not state_pos:
            log.warning("Exchange has open position not in state — check Binance manually.")
        elif not exch_pos and state_pos:
            log.warning("State has position but exchange doesn't — clearing state position.")
            state["position"] = None
            save_state(state)

    bot = QuantBot(ex, state, cm)
    candle_n = 0
    log.info("Bot running — waiting for first candle close...")

    while True:
        try:
            wait_for_candle()

            df = ex.fetch_candles()
            df = compute_indicators(df)
            df = df.iloc[WARMUP:]   # trim warmup candles

            latest_ts = str(df.index[-1])

            # Skip duplicate candle
            if latest_ts == state.get("last_candle_ts"):
                time.sleep(10)
                continue

            state["last_candle_ts"]  = latest_ts
            state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
            candle_n += 1

            bot.process(df, candle_n)

            # Print dashboard every hour (4 × 15m candles)
            if candle_n % 4 == 0:
                bot.dashboard(df)

            save_state(state)
            time.sleep(2)

        except ccxt.NetworkError as e:
            log.warning(f"Network error: {e} — retry in 30s")
            time.sleep(30)

        except ccxt.ExchangeError as e:
            log.error(f"Exchange error: {e} — retry in 60s")
            time.sleep(60)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            try:
                df = ex.fetch_candles()
                df = compute_indicators(df)
                bot.dashboard(df)
            except Exception:
                pass
            save_state(state)
            cm.save_state()
            break

        except Exception as e:
            log.error(f"Unexpected error: {e}\n{traceback.format_exc()}")
            log.info("Resuming in 60s...")
            time.sleep(60)


# ══════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="QuantBot — BTC Futures Trading Bot")
    ap.add_argument("--live",   action="store_true",
                    help="Enable live trading (requires env API keys)")
    ap.add_argument("--status", action="store_true",
                    help="Print current status and exit")
    ap.add_argument("--reset",  action="store_true",
                    help="Delete all state files and start fresh")
    args = ap.parse_args()

    if args.status:
        cmd_status()
        sys.exit(0)

    if args.reset:
        reset_files = [
            STATE_FILE, CORPUS_STATE_FILE, TRADE_LOG_FILE, LOG_FILE, BOT_PAUSED_FILE,
            # Shared with notifier.py — also part of a "fresh start"
            os.path.join(DATA_DIR, "rsi_alert_state.json"),
            os.path.join(DATA_DIR, "rsi_history.json"),
            os.path.join(DATA_DIR, "notifier.log"),
        ]
        for f in reset_files:
            p = Path(f)
            if p.exists():
                p.unlink()
                print(f"Deleted {f}")
        print("State cleared. Run 'python bot.py' to start fresh.")
        sys.exit(0)

    main(go_live=args.live)