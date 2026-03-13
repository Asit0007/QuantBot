#!/usr/bin/env python3
"""
notifier.py — QuantBot Telegram Notifier
════════════════════════════════════════════════════════════════════
Sends Telegram alerts for:
  • Every trade (entry + exit with full details)
  • RSI extremes on BTC ETH SOL BNB XRP SUI
    — Monthly timeframe normally
    — Weekly if < 100 candles available (new coins)
    — Alert when RSI < 20 or > 80
    — No spam: only fires when RSI crosses threshold (state tracked)
  • Bot heartbeat — if bot_state.json stops updating → CRASH ALERT
  • Daily summary at 00:00 UTC
  • Remote commands: /status /pause /resume /balance /pos

SETUP:
  1. Message @BotFather on Telegram, create a bot, copy the token
  2. Message @userinfobot to get your chat ID
  3. Add to .env:
       TELEGRAM_TOKEN=123456:ABCdefGHI...
       TELEGRAM_CHAT_ID=987654321
  4. Run alongside bot.py (in Docker this is automatic):
       python notifier.py

ARCHITECTURE:
  - Watches trade_log.csv for new rows  → trade alerts
  - Polls bot_state.json every 60s      → heartbeat check
  - Scans coin RSIs every 4 hours       → RSI alerts
  - Receives Telegram commands via polling (no webhook needed)
════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import ccxt
import pandas as pd
import requests
from ta.momentum import RSIIndicator

# ── Config from .env ──────────────────────────────────────────────
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN",  "")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID","")
DATA_DIR    = os.getenv("DATA_DIR", ".")
STATE_FILE  = os.path.join(DATA_DIR, "bot_state.json")
CORPUS_FILE = os.path.join(DATA_DIR, "corpus_state.json")
TRADE_LOG   = os.path.join(DATA_DIR, "trade_log.csv")
RSI_STATE   = os.path.join(DATA_DIR, "rsi_alert_state.json")
RSI_HISTORY = os.path.join(DATA_DIR, "rsi_history.json")   # full scan log — read by dashboard
BOT_PAUSED  = os.path.join(DATA_DIR, "bot_paused.flag")
LOG_FILE    = os.path.join(DATA_DIR, "notifier.log")

# Intervals
HEARTBEAT_INTERVAL = 60        # seconds — how often to check bot is alive
HEARTBEAT_TIMEOUT  = 30        # minutes — max allowed silence before crash alert
RSI_SCAN_INTERVAL  = 4 * 3600  # seconds — how often to scan RSI (4 hours)
DAILY_SUMMARY_HOUR = 0         # UTC hour for daily summary (midnight)

# RSI thresholds
RSI_OVERSOLD   = 20
RSI_OVERBOUGHT = 80
MIN_CANDLES    = 100   # use weekly if monthly candle count < this
RSI_PERIOD     = 14

# Coins to scan (spot pairs — monthly/weekly RSI for macro context)
SCAN_COINS = {
    "BTC/USDT": "BTC",
    "ETH/USDT": "ETH",
    "SOL/USDT": "SOL",
    "BNB/USDT": "BNB",
    "XRP/USDT": "XRP",
    "SUI/USDT": "SUI",
}

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("notifier")


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM API
# ══════════════════════════════════════════════════════════════════

def tg(method: str, **params) -> dict | None:
    """Call Telegram Bot API."""
    if not TOKEN:
        return None
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                             json=params, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            log.warning(f"Telegram error: {data}")
        return data
    except Exception as e:
        log.error(f"Telegram request failed: {e}")
        return None


def send(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to the configured chat."""
    if not TOKEN or not CHAT_ID:
        log.info(f"[MSG suppressed — no token/chat_id] {text[:80]}")
        return
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode=parse_mode)
    log.info(f"Sent: {text[:80]}...")


def get_updates(offset: int = 0) -> list:
    data = tg("getUpdates", offset=offset, timeout=5, limit=10)
    if data and data.get("ok"):
        return data.get("result", [])
    return []


# ══════════════════════════════════════════════════════════════════
#  MESSAGE TEMPLATES
# ══════════════════════════════════════════════════════════════════

def msg_trade_open(pos: dict, corpus: float) -> str:
    side  = pos.get("side","?").upper()
    ep    = pos.get("entry_price", 0)
    stop  = pos.get("stop_price", 0)
    qty   = pos.get("quantity", 0)
    mgn   = pos.get("margin", 0)
    dist  = abs(ep - stop) / ep * 100 if ep else 0
    icon  = "🟢" if side == "LONG" else "🔴"
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{icon} <b>{side} OPENED</b>\n"
        f"──────────────────\n"
        f"⏰ {ts}\n"
        f"💰 Entry:    <code>${ep:,.2f}</code>\n"
        f"🛑 Stop:     <code>${stop:,.2f}</code>  ({dist:.2f}% away)\n"
        f"📦 Qty:      <code>{qty:.6f} BTC</code>\n"
        f"💼 Margin:   <code>${mgn:.2f}</code>\n"
        f"📊 Corpus:   <code>${corpus:.2f}</code>"
    )


def msg_trade_close(trade_row: pd.Series, state: dict, corpus: float) -> str:
    side   = str(trade_row.get("side","?")).upper()
    entry  = float(trade_row.get("entry_price", 0))
    exit_p = float(trade_row.get("exit_price", 0))
    pnl    = float(trade_row.get("pnl_usd", 0))
    fees   = float(trade_row.get("fees_usd", 0))
    bal    = float(trade_row.get("balance", 0))
    reason = str(trade_row.get("reason","?"))
    hold   = int(trade_row.get("hold_candles", 0))
    mode   = str(trade_row.get("mode","?"))
    ts     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n      = state.get("total_trades", 0)
    wins   = state.get("total_wins", 0)
    wr     = wins / n * 100 if n > 0 else 0
    win    = pnl > 0
    icon   = "🏆" if win else "💀"
    pnl_s  = f"+${pnl:,.2f}" if win else f"-${abs(pnl):,.2f}"
    move_p = abs(exit_p - entry) / entry * 100 if entry else 0
    hold_h = hold * 0.25
    return (
        f"{icon} <b>{side} {'WIN' if win else 'LOSS'}</b>  {pnl_s}\n"
        f"──────────────────\n"
        f"⏰ {ts}  [{mode.upper()}]\n"
        f"📥 Entry:   <code>${entry:,.2f}</code>\n"
        f"📤 Exit:    <code>${exit_p:,.2f}</code>  ({move_p:.2f}% move)\n"
        f"📋 Reason:  {reason}\n"
        f"⏱  Hold:    {hold_h:.1f}h  ({hold} candles)\n"
        f"💸 Fees:    <code>${fees:.2f}</code>\n"
        f"──────────────────\n"
        f"💼 Balance: <code>${bal:,.2f}</code>\n"
        f"📊 Corpus:  <code>${corpus:.2f}</code>\n"
        f"📈 WR:      {wr:.1f}%  ({wins}/{n} trades)"
    )


def msg_rsi_alert(coin: str, rsi: float, tf: str, price: float) -> str:
    zone  = "OVERSOLD 📉" if rsi <= RSI_OVERSOLD else "OVERBOUGHT 📈"
    icon  = "🔵" if rsi <= RSI_OVERSOLD else "🔴"
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{icon} <b>{coin} RSI EXTREME — {zone}</b>\n"
        f"──────────────────\n"
        f"⏰ {ts}\n"
        f"📊 Timeframe: <b>{tf}</b>\n"
        f"💡 RSI:       <code>{rsi:.1f}</code>  "
        f"(threshold: {RSI_OVERSOLD if rsi<=RSI_OVERSOLD else RSI_OVERBOUGHT})\n"
        f"💰 Price:     <code>${price:,.4f}</code>\n"
        f"{'🛒 Potential buy opportunity' if rsi<=RSI_OVERSOLD else '🚨 Potential sell/short opportunity'}"
    )


def msg_crash_alert(minutes_silent: float) -> str:
    # Commands reference Docker — bot runs inside docker-compose
    return (
        f"🚨 <b>BOT CRASH / STALL ALERT</b>\n"
        f"──────────────────\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"❌ No candle updates for <b>{minutes_silent:.0f} minutes</b>\n"
        f"🔧 SSH to server and check:\n"
        f"   <code>docker-compose logs bot --tail=50</code>\n"
        f"   <code>docker-compose restart bot</code>"
    )


def msg_daily_summary(state: dict, corpus: dict, trades: pd.DataFrame) -> str:
    bal     = state.get("balance", 0)
    n       = state.get("total_trades", 0)
    wins    = state.get("total_wins", 0)
    wr      = wins / n * 100 if n > 0 else 0
    pnl_tot = state.get("total_pnl", 0)
    corp    = corpus.get("corpus", bal)
    dca     = corpus.get("total_dca_added", 0)
    inv     = state.get("start_balance", 100) + dca
    ret     = (bal - inv) / inv * 100 if inv > 0 else 0
    date    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = 0.0
    if not trades.empty and "pnl_usd" in trades.columns:
        try:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            mask      = trades["datetime"].dt.strftime("%Y-%m-%d") == today_str
            today_pnl = float(trades.loc[mask, "pnl_usd"].sum())
        except Exception:
            today_pnl = 0.0
    cb_status = "Active 🛑" if state.get("cb_pause_until") else "Clear ✅"
    return (
        f"📊 <b>Daily Summary — {date}</b>\n"
        f"══════════════════\n"
        f"💰 Balance:    <code>${bal:,.2f}</code>\n"
        f"📈 Return:     <code>{ret:+.1f}%</code>  (on ${inv:.2f} invested)\n"
        f"📊 Corpus:     <code>${corp:.2f}</code>\n"
        f"💵 Today P&L:  <code>${today_pnl:+.2f}</code>\n"
        f"💵 Total P&L:  <code>${pnl_tot:+.2f}</code>\n"
        f"──────────────────\n"
        f"🎯 Trades:     {n}  |  Wins: {wins}  |  WR: {wr:.1f}%\n"
        f"🛑 CB Status:  {cb_status}\n"
        f"💸 DCA Added:  <code>${dca:.2f}</code>"
    )


def msg_status(state: dict, corpus: dict) -> str:
    bal  = state.get("balance", 0)
    n    = state.get("total_trades", 0)
    wins = state.get("total_wins", 0)
    wr   = wins / n * 100 if n > 0 else 0
    corp = corpus.get("corpus", bal)
    mode = state.get("mode", "?").upper()
    pos  = state.get("position")
    cb   = state.get("cb_pause_until")
    cl   = state.get("consecutive_losses", 0)
    pos_str = "NONE"
    if pos:
        side    = pos.get("side","?").upper()
        ep      = pos.get("entry_price",0)
        stop    = pos.get("stop_price",0)
        pos_str = f"{side} @ ${ep:,.0f}  stop ${stop:,.0f}"
    cb_str = "Clear"
    if cb:
        try:
            res = datetime.fromisoformat(cb)
            hrs = max(0, (res - datetime.now(timezone.utc)).total_seconds() / 3600)
            cb_str = f"PAUSED  {hrs:.1f}h left" if hrs > 0 else "Clear"
        except Exception:
            cb_str = "?"
    return (
        f"📊 <b>Bot Status  [{mode}]</b>\n"
        f"──────────────────\n"
        f"💰 Balance:   <code>${bal:,.2f}</code>\n"
        f"📊 Corpus:    <code>${corp:.2f}</code>\n"
        f"🎯 Trades:    {n}  WR {wr:.1f}%\n"
        f"📍 Position:  {pos_str}\n"
        f"🛑 CB:        {cb_str}\n"
        f"❌ Consec L:  {cl}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ══════════════════════════════════════════════════════════════════
#  FILE HELPERS
# ══════════════════════════════════════════════════════════════════

def read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_trades() -> pd.DataFrame:
    try:
        if not Path(TRADE_LOG).exists():
            return pd.DataFrame()
        df = pd.read_csv(TRADE_LOG)
        if df.empty:
            return pd.DataFrame()
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        if "pnl_usd" not in df.columns and "pnl" in df.columns:
            df["pnl_usd"] = df["pnl"]
        return df
    except Exception:
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
#  TRADE WATCHER  (watches trade_log.csv for new rows)
# ══════════════════════════════════════════════════════════════════

class TradeWatcher:
    def __init__(self):
        self._last_count  = self._current_count()
        self._last_pos_ts = None

    def _current_count(self) -> int:
        try:
            return len(pd.read_csv(TRADE_LOG)) if Path(TRADE_LOG).exists() else 0
        except Exception:
            return 0

    def check(self):
        state  = read_json(STATE_FILE)
        corpus = read_json(CORPUS_FILE)
        corp_v = corpus.get("corpus", state.get("balance", 0))

        # Check for new open position
        pos = state.get("position")
        if pos:
            pos_ts = pos.get("entry_time", "")
            if pos_ts != self._last_pos_ts:
                self._last_pos_ts = pos_ts
                try:
                    send(msg_trade_open(pos, corp_v))
                    log.info("Trade OPEN notification sent")
                except Exception as e:
                    log.error(f"Open notification error: {e}")
        else:
            self._last_pos_ts = None

        # Check for new closed trades
        new_count = self._current_count()
        if new_count > self._last_count:
            try:
                df = load_trades()
                if not df.empty:
                    new_rows = df.tail(new_count - self._last_count)
                    for _, row in new_rows.iterrows():
                        send(msg_trade_close(row, state, corp_v))
                        log.info(f"Trade CLOSE notification sent — P&L ${row.get('pnl_usd',0):+.2f}")
            except Exception as e:
                log.error(f"Close notification error: {e}")
            self._last_count = new_count


# ══════════════════════════════════════════════════════════════════
#  HEARTBEAT MONITOR
# ══════════════════════════════════════════════════════════════════

class HeartbeatMonitor:
    def __init__(self):
        self._last_updated_at = None
        self._alert_sent      = False

    def check(self):
        state      = read_json(STATE_FILE)
        updated_at = state.get("last_updated_at")
        if updated_at is None:
            return   # bot hasn't processed its first candle yet

        if updated_at != self._last_updated_at:
            # Bot updated — reset alert state
            self._last_updated_at = updated_at
            self._alert_sent      = False
            return

        # Timestamp unchanged — measure wall-clock silence
        try:
            last  = pd.to_datetime(updated_at, utc=True)
            now   = pd.Timestamp.now(tz="UTC")
            mins  = (now - last).total_seconds() / 60
            if mins > HEARTBEAT_TIMEOUT and not self._alert_sent:
                send(msg_crash_alert(mins))
                self._alert_sent = True
                log.warning(f"Crash alert sent — {mins:.0f}m since last state update")
        except Exception as e:
            log.error(f"Heartbeat check error: {e}")


# ══════════════════════════════════════════════════════════════════
#  RSI SCANNER
# ══════════════════════════════════════════════════════════════════

class RSIScanner:
    def __init__(self):
        self._state = self._load_state()
        # Use spot exchange — we scan monthly/weekly candles for macro RSI context
        self._exchange = ccxt.binance({"enableRateLimit": True})
        try:
            self._exchange.load_markets()
        except Exception as e:
            log.warning(f"RSI scanner exchange init: {e}")

    def _load_state(self) -> dict:
        try:
            with open(RSI_STATE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            with open(RSI_STATE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            pass

    def _fetch_ohlcv(self, symbol: str, tf: str, limit: int = 200) -> pd.DataFrame | None:
        try:
            raw = self._exchange.fetch_ohlcv(symbol, tf, limit=limit)
            df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df
        except Exception as e:
            log.warning(f"OHLCV fetch failed {symbol} {tf}: {e}")
            return None

    def scan(self):
        log.info("RSI scan running...")
        for raw_symbol, coin in SCAN_COINS.items():
            try:
                self._scan_coin(raw_symbol, coin)
                time.sleep(0.5)
            except Exception as e:
                log.error(f"RSI scan error {coin}: {e}")
        self._save_state()
        log.info("RSI scan complete")

    def _scan_coin(self, raw_symbol: str, coin: str):
        # Resolve symbol (spot)
        symbol = None
        for c in [raw_symbol, raw_symbol.replace("/","")]:
            if c in self._exchange.symbols:
                symbol = c
                break
        if not symbol:
            log.info(f"  {coin}: symbol not found, skipping")
            return

        df_m = self._fetch_ohlcv(symbol, "1M", 200)
        if df_m is None:
            return

        if len(df_m) >= MIN_CANDLES:
            tf_used, df_use = "Monthly", df_m
        else:
            log.info(f"  {coin}: {len(df_m)} monthly candles → using Weekly")
            # Binance spot uses lowercase "1w" — "1W" is futures-only syntax
            df_use = self._fetch_ohlcv(symbol, "1w", 200)
            tf_used = "Weekly"
            if df_use is None:
                return

        self._evaluate(coin, df_use, tf_used)

    def _evaluate(self, coin: str, df: pd.DataFrame, tf: str):
        if len(df) < RSI_PERIOD + 2:
            log.info(f"  {coin}: not enough candles for RSI")
            return

        rsi_val   = float(RSIIndicator(close=df["close"], window=RSI_PERIOD).rsi().dropna().iloc[-1])
        price     = float(df["close"].iloc[-1])
        state_key = f"{coin}_{tf}"
        log.info(f"  {coin} [{tf}]: RSI={rsi_val:.1f}  price=${price:,.4f}")

        # ── Persist every reading to rsi_history.json for the dashboard ──
        zone = None
        if rsi_val <= RSI_OVERSOLD:
            zone = "oversold"
        elif rsi_val >= RSI_OVERBOUGHT:
            zone = "overbought"

        self._append_history(coin, tf, rsi_val, price, zone)

        # ── Alert logic (only for extremes, no duplicate alerts) ──────────
        if zone is None:
            self._state.pop(state_key, None)   # back to neutral — re-arms next time
            return

        if self._state.get(state_key) == zone:
            log.info(f"  {coin}: already alerted for {zone}, skipping")
            return

        send(msg_rsi_alert(coin, rsi_val, tf, price))
        self._state[state_key] = zone
        log.info(f"  {coin}: RSI alert sent ({zone}  {rsi_val:.1f})")

    def _append_history(self, coin: str, tf: str, rsi: float, price: float, zone: str | None):
        """Append one RSI reading to rsi_history.json — dashboard reads this file."""
        try:
            try:
                with open(RSI_HISTORY) as f:
                    history = json.load(f)
            except Exception:
                history = []

            history.append({
                "ts":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "coin":  coin,
                "tf":    tf,
                "rsi":   round(rsi, 1),
                "price": price,
                "zone":  zone or "neutral",
            })

            # Keep last 2000 entries (~1 year at 4h scans × 6 coins)
            if len(history) > 2000:
                history = history[-2000:]

            with open(RSI_HISTORY, "w") as f:
                json.dump(history, f)
        except Exception as e:
            log.error(f"RSI history write error: {e}")


# ══════════════════════════════════════════════════════════════════
#  DAILY SUMMARY SCHEDULER
# ══════════════════════════════════════════════════════════════════

class DailySummary:
    def __init__(self):
        self._last_day = -1

    def check(self):
        now = datetime.now(timezone.utc)
        if now.hour == DAILY_SUMMARY_HOUR and now.day != self._last_day:
            try:
                send(msg_daily_summary(read_json(STATE_FILE),
                                       read_json(CORPUS_FILE),
                                       load_trades()))
                self._last_day = now.day
                log.info("Daily summary sent")
            except Exception as e:
                log.error(f"Daily summary error: {e}")


# ══════════════════════════════════════════════════════════════════
#  COMMAND HANDLER  (Telegram → bot control)
# ══════════════════════════════════════════════════════════════════

class CommandHandler:
    def __init__(self):
        self._offset = 0

    def poll(self):
        for u in get_updates(self._offset):
            self._offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if msg:
                self._handle(msg.get("text","").strip().lower())

    def _handle(self, text: str):
        state  = read_json(STATE_FILE)
        corpus = read_json(CORPUS_FILE)

        if text in ("/status", "/s"):
            send(msg_status(state, corpus))

        elif text in ("/balance", "/bal", "/b"):
            bal  = state.get("balance", 0)
            corp = corpus.get("corpus", bal)
            dca  = corpus.get("total_dca_added", 0)
            inv  = state.get("start_balance", 100) + dca
            ret  = (bal - inv) / inv * 100 if inv > 0 else 0
            send(f"💰 Balance: <code>${bal:,.2f}</code>\n"
                 f"📊 Corpus:  <code>${corp:,.2f}</code>\n"
                 f"📈 Return:  <code>{ret:+.1f}%</code>")

        elif text in ("/pos", "/position", "/p"):
            pos = state.get("position")
            if pos:
                side = pos.get("side","?").upper()
                ep   = pos.get("entry_price", 0)
                stop = pos.get("stop_price", 0)
                qty  = pos.get("quantity", 0)
                send(f"📍 <b>Open {side}</b>\n"
                     f"Entry ${ep:,.2f} | Stop ${stop:,.2f} | Qty {qty:.6f}")
            else:
                send("📍 No open position")

        elif text == "/pause":
            Path(BOT_PAUSED).touch()
            send("⏸ <b>Pause flag set.</b>\nBot will skip new entries until /resume.\n"
                 "<i>Note: existing positions still exit normally.</i>")
            log.info("Bot paused via Telegram command")

        elif text == "/resume":
            if Path(BOT_PAUSED).exists():
                Path(BOT_PAUSED).unlink()
                send("▶️ <b>Resumed.</b> Bot will take new signals.")
                log.info("Bot resumed via Telegram command")
            else:
                send("▶️ Bot was not paused.")

        elif text in ("/help", "/h", "/start"):
            send(
                "🤖 <b>QuantBot Commands</b>\n"
                "──────────────────\n"
                "/status  — full status\n"
                "/balance — balance + return\n"
                "/pos     — open position\n"
                "/pause   — pause new entries\n"
                "/resume  — resume entries\n"
                "/help    — this message"
            )
        elif text.startswith("/"):
            send(f"Unknown command: {text}\nTry /help")


# ══════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════

def main():
    if not TOKEN:
        print("⚠️  TELEGRAM_TOKEN not set — messages suppressed, monitoring continues")
    if not CHAT_ID:
        print("⚠️  TELEGRAM_CHAT_ID not set")

    log.info("━" * 56)
    log.info("  QuantBot Notifier starting")
    log.info(f"  Data dir:           {DATA_DIR}")
    log.info(f"  Heartbeat timeout:  {HEARTBEAT_TIMEOUT} min")
    log.info(f"  RSI scan interval:  {RSI_SCAN_INTERVAL//3600} hours")
    log.info(f"  Daily summary:      {DAILY_SUMMARY_HOUR:02d}:00 UTC")
    log.info("━" * 56)

    watcher   = TradeWatcher()
    heartbeat = HeartbeatMonitor()
    daily     = DailySummary()
    commands  = CommandHandler()
    rsi       = RSIScanner()

    send("✅ <b>QuantBot Notifier started</b>\n"
         f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
         "Commands: /status /balance /pos /pause /resume")

    last_rsi_scan = time.time() - RSI_SCAN_INTERVAL + 30   # first scan 30s after start

    while True:
        try:
            watcher.check()
            heartbeat.check()
            commands.poll()
            daily.check()
            if time.time() - last_rsi_scan >= RSI_SCAN_INTERVAL:
                rsi.scan()
                last_rsi_scan = time.time()
            time.sleep(HEARTBEAT_INTERVAL)

        except KeyboardInterrupt:
            log.info("Notifier stopped by user.")
            send("⛔ QuantBot Notifier stopped.")
            break

        except Exception as e:
            log.error(f"Main loop error: {e}\n{traceback.format_exc()}")
            time.sleep(30)


if __name__ == "__main__":
    main()