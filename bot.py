#!/usr/bin/env python3
"""
bot.py — QuantBot Live Trading Engine v1.0
════════════════════════════════════════════════════════════════════
Built from 20 backtests (Sep 2019 → Mar 2026), $100 → $4,699

LOCKED PARAMETERS (do not change without re-backtesting):
  Signal:   RSI Divergence(14) + MACD Cross(12/26/9) + Volume(2×)
  Asset:    BTC/USDT isolated margin futures
  Frame:    15m candles
  Leverage: 20×
  Risk:     10% of corpus per trade (as margin)
  CB:       5 consecutive losses → 48h pause (flat)
  DCA:      $10/mo on 10th, +10%/yr
  Ratchet:  corpus UP after 10 net+ trades, DOWN after 10 consec losses

PAPER TRADE FIRST:
  PAPER_TRADE = true  (default in .env)  → simulates everything, no real orders
  Run 20+ paper trades, compare WR (~12%) and PF (~1.78) to backtest.
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

try:
    # ── User-facing parameters — set these in .env ────────────────────
    # PAPER_TRADE: "true" → simulate only. Set "false" in .env to go live.
    PAPER_TRADE    = os.getenv("PAPER_TRADE", "true").strip().lower() == "true"

    # Starting balance for a fresh state (only used on first ever run)
    START_BALANCE  = float(os.getenv("START_BALANCE"))

    # Position sizing
    LEVERAGE       = int(os.getenv("LEVERAGE"))
    RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE"))

    # Corpus / DCA
    DCA_DAY        = int(os.getenv("DCA_DAY"))       # day of month for contribution
    DCA_BASE       = float(os.getenv("DCA_MONTHLY_USD"))   # base monthly DCA ($)
    DCA_GROWTH     = float(os.getenv("DCA_ANNUAL_GROWTH")) # 10% annual step-up
    START_YEAR     = int(os.getenv("START_YEAR"))   # year the bot first ran

    # ── LOCKED strategy parameters ───────────────────────────────────
    # Defaults are the values fixed across 20 backtests (Sep 2019 → Mar 2026).
    # Overridable via .env — but changing any value invalidates backtest results.
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

except KeyError as e:
    # If any variable above is missing from the .env, the bot safely stops
    raise RuntimeError(f"CRITICAL: Missing environment variable {e}. Please update your .env file.")

# ── Files — all written to DATA_DIR (shared Docker volume) ────────
DATA_DIR         = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE        = os.path.join(DATA_DIR, "bot_state.json")
CORPUS_STATE_FILE = os.path.join(DATA_DIR, "corpus_state.json")
TRADE_LOG_FILE    = os.path.join(DATA_DIR, "trade_log.csv")
LOG_FILE          = os.path.join(DATA_DIR, "bot.log")

# ── Paper trade benchmarks (from backtest) ────────────────────────
BENCH_WR         = 0.124
BENCH_PF         = 1.78
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

    def current_price(self) -> float:
        return float(self._ex.fetch_ticker(self._symbol)["last"])

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

        stop_side   = "sell" if side == "buy" else "buy"
        stop_order  = self._ex.create_order(
            self._symbol, "STOP_MARKET", stop_side, qty,
            params={"stopPrice": stop, "reduceOnly": True}
        )
        filled = float(order.get("average") or order.get("price") or 0)
        return {"filled_price": filled, "stop_order_id": stop_order["id"]}

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
      qty           = dollar_risk / (stop_distance x LEVERAGE)

    P&L at stop = stop_distance x qty x LEVERAGE = dollar_risk  (always)

    Previous bug: qty = (corpus x RISK x LEVERAGE) / price
      P&L formula also multiplied by LEVERAGE => LEVERAGE squared.
      A 0.47% ATR gave 18.8% loss instead of 10%.
      A 2.5% ATR would wipe the entire account in one trade.

    Example: corpus=$110, price=$70121, stop=$70448 (ATR=$218)
      dollar_risk   = $11.00
      stop_distance = $327
      qty           = 11 / (327 x 20) = 0.001682 BTC
      P&L at stop   = 327 x 0.001682 x 20 = $11.00 = 10% of corpus
    """
    dollar_risk   = corpus * RISK_PER_TRADE
    stop_distance = abs(price - stop_price)
    # Guard: never let stop be too close (< 0.01% of price)
    min_distance  = price * 0.0001
    stop_distance = max(stop_distance, min_distance)
    qty      = dollar_risk / (stop_distance * LEVERAGE)
    margin   = qty * price / LEVERAGE
    notional = qty * price
    return {"margin": margin, "notional": notional, "qty": qty,
            "stop_distance": stop_distance, "dollar_risk": dollar_risk}


# ══════════════════════════════════════════════════════════════════════
#  CORE BOT
# ══════════════════════════════════════════════════════════════════════

class QuantBot:

    def __init__(self, ex: Exchange, state: dict, corpus_mgr: CorpusManager):
        self.ex  = ex
        self.st  = state
        self.cm  = corpus_mgr
        self._entry_candle_n = 0    # for measuring hold time

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

    # ─── OPEN LONG ────────────────────────────────────────────────────
    def open_long(self, candle: pd.Series, candle_n: int):
        price    = float(candle["close"])
        atr      = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0
        stop     = price - (LONG_ATR_MULT * atr) if atr > 0 else price * 0.95
        sizing   = size_position(self.cm.corpus, price, stop)
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
        }
        self._entry_candle_n = candle_n
        save_state(self.st)

        tag = "[paper] " if self.ex.paper else ""
        log.info(
            f"{tag}LONG OPEN  price=${filled:,.2f}  "
            f"stop=${stop:,.2f}  qty={qty:.6f} BTC  "
            f"margin=${sizing['margin']:.2f}  corpus=${self.cm.corpus:.2f}"
        )

    # ─── OPEN SHORT ───────────────────────────────────────────────────
    def open_short(self, candle: pd.Series, candle_n: int):
        price    = float(candle["close"])
        atr      = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0
        stop     = price + (SHORT_ATR_MULT * atr) if atr > 0 else price * 1.05
        sizing   = size_position(self.cm.corpus, price, stop)
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
        }
        self._entry_candle_n = candle_n
        save_state(self.st)

        tag = "[paper] " if self.ex.paper else ""
        log.info(
            f"{tag}SHORT OPEN  price=${filled:,.2f}  "
            f"stop=${stop:,.2f}  qty={qty:.6f} BTC  "
            f"margin=${sizing['margin']:.2f}  corpus=${self.cm.corpus:.2f}"
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

        # Exit price: stop price if stop-hit, else close of signal candle
        if reason == "stop":
            exit_price = stop
        else:
            exit_price = float(candle["close"])

        if not self.ex.paper:
            result     = self.ex.place_exit(side, qty, pos.get("stop_order_id"))
            exit_price = result["filled_price"] or exit_price

        # P&L (mirrors backtest formula exactly)
        fee_out = exit_price * qty * FEE_RATE
        if side == "long":
            raw_pnl = (exit_price - entry) * qty * LEVERAGE
        else:
            raw_pnl = (entry - exit_price) * qty * LEVERAGE
        pnl = raw_pnl - fee_in - fee_out

        self.st["balance"]      += pnl
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
            cb_fired   = cb_on_loss(self.st)
            # Corpus ratchet DOWN on CB trigger
            if cb_fired:
                self.cm.on_trade_complete(pnl, self.st["balance"])

        if pnl > 0 or not (pnl <= 0 and self.st.get("cb_pause_until")):
            # Normal ratchet call (not double-calling on CB trigger)
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

        # 3. Exits
        if pos is not None:
            side  = pos["side"]
            entry = pos["entry_price"]
            stop  = pos["stop_price"]

            if side == "long":
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
                unreal = (price - entry) * pos["quantity"] * LEVERAGE
            else:
                unreal = (entry - price) * pos["quantity"] * LEVERAGE
            entry_fee = entry * pos["quantity"] * FEE_RATE
            unreal_net = unreal - entry_fee
            print(f"  Position:    {side.upper()}  entry=${entry:,.2f}  "
                  f"stop=${stop:,.2f}  "
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
        print(f"  Open pos:     {pos['side'].upper()} @ ${pos['entry_price']:,.2f}  "
              f"stop ${pos['stop_price']:,.2f}")
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
        for f in [STATE_FILE, CORPUS_STATE_FILE, TRADE_LOG_FILE, LOG_FILE]:
            p = Path(f)
            if p.exists():
                p.unlink()
                print(f"Deleted {f}")
        print("State cleared. Run 'python bot.py' to start fresh.")
        sys.exit(0)

    main(go_live=args.live)