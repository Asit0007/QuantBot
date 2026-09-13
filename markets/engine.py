"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/engine.py — the universe-aware trading loop                  ║
╠══════════════════════════════════════════════════════════════════════╣
║  A faithful port of bot.py's QuantBot.process() (bot.py:1154) with    ║
║  two changes, both structural rather than behavioural:                ║
║                                                                      ║
║  1. ONE SYMBOL -> A UNIVERSE. State holds `positions` keyed by symbol ║
║     rather than a single `position`. For a universe of one this is    ║
║     indistinguishable from bot.py, which is exactly what the replay   ║
║     test exploits: same candles, same config, same trades.            ║
║                                                                      ║
║  2. `now` IS A PARAMETER, NEVER datetime.now(). bot.py reads the wall ║
║     clock inside cb_is_paused(), cb_on_loss() and check_dca(). That   ║
║     is correct live and IMPOSSIBLE to replay — a 48h circuit-breaker  ║
║     pause measured against today's clock never expires when you are   ║
║     feeding it candles from 2021, so a replay would silently stop     ║
║     trading after the first five-loss streak and diverge from the     ║
║     backtest for a reason that has nothing to do with the strategy.   ║
║     Threading `now` through makes live and replay the same code path. ║
║                                                                      ║
║  ── ORDER OF OPERATIONS IS LOAD-BEARING ──────────────────────────── ║
║  DCA -> snapshot corpus -> update armed -> funding -> exits -> entries║
║                                                                      ║
║  The corpus snapshot is the subtle one. An exit and a fresh entry can ║
║  happen on the same candle, and reading cm.corpus at entry time lets  ║
║  the exit's ratchet inflate the entry behind it on the same bar.      ║
║  Measured cost of getting it wrong: PF 1.63 -> 1.59 over 6.9 years.   ║
║  Reordering these steps silently invalidates every backtest.          ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import MarketConfig
from .state import armed_for
from .strategy import (fit_to_margin, liq_price, require_exit_model_safe,
                       size_position_nostop, size_position_stop)


class MarketEngine:
    """Runs one market's universe. Venue access goes through `adapter`."""

    def __init__(self, cfg: MarketConfig, adapter, corpus_manager,
                 state: dict, *, exit_model: str = "nostop", paper: bool = True,
                 on_trade=None):
        require_exit_model_safe(cfg, exit_model)   # refuses nostop on a gapping market
        self.cfg = cfg
        self.ex = adapter
        self.cm = corpus_manager
        self.st = state
        self.exit_model = exit_model
        self.nostop = exit_model == "nostop"
        self.paper = paper
        self.on_trade = on_trade          # callback(trade_dict) — the trade log sink
        self._corpus_this_candle = None
        self._entry_candle_n: dict[str, int] = {}

    # ── circuit breaker (candle-time, not wall-clock) ────────────────
    def cb_is_paused(self, now: datetime) -> tuple[bool, float]:
        ts = self.st.get("cb_pause_until")
        if not ts:
            return False, 0.0
        resume = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        if now >= resume:
            self.st["cb_pause_until"] = None
            return False, 0.0
        return True, (resume - now).total_seconds() / 3600

    def cb_on_loss(self, now: datetime) -> bool:
        self.st["consecutive_losses"] += 1
        if self.st["consecutive_losses"] >= self.cfg.cb_trigger:
            self.st["cb_pause_until"] = (
                now + timedelta(hours=self.cfg.cb_hours)).isoformat()
            self.st["consecutive_losses"] = 0
            return True
        return False

    # ── DCA ──────────────────────────────────────────────────────────
    def check_dca(self, now: datetime) -> None:
        if now.day != self.cfg.dca_day:
            return
        key = [now.year, now.month]
        if self.st.get("last_dca_month") == key:
            return
        res = self.cm.on_monthly_refresh(self.st["balance"], now.year,
                                         now.month, self.cfg.start_year)
        if res["contribution"] > 0:
            self.st["balance"] += res["contribution"]
            self.st["last_dca_month"] = key

    # ── funding (paper only; live venues debit margin themselves) ────
    def charge_funding(self, symbol: str, candle: pd.Series) -> None:
        pos = self.st["positions"].get(symbol)
        if pos is None or not self.paper:
            return
        ts = candle.name
        fn = getattr(self.ex, "funding_in_window", None)
        if fn is None:
            return                       # venue has no funding concept (equities)
        try:
            rates = fn(symbol, ts, ts + timedelta(minutes=self.cfg.candle_minutes))
        except Exception:
            return                       # unavailable -> skip, same as bot.py
        price = float(candle["close"])
        for rate in rates:
            cost = rate * pos["quantity"] * price
            if pos["side"] == "short":
                cost = -cost             # shorts receive when rate > 0
            self.st["balance"] -= cost
            pos["funding_paid"] = pos.get("funding_paid", 0.0) + cost

    # ── sizing ───────────────────────────────────────────────────────
    def _size_or_skip(self, symbol: str, price: float, stop: float,
                      side: str) -> dict | None:
        corpus = (self._corpus_this_candle if self._corpus_this_candle is not None
                  else self.cm.corpus)
        base = (size_position_nostop(corpus, price, self.cfg) if self.nostop
                else size_position_stop(corpus, price, stop, self.cfg))
        sizing = fit_to_margin(base, self.st["balance"], self.cfg)

        spec = self.ex.instrument(symbol)
        qty = spec.round_qty(sizing["qty"])
        if spec.rejects(qty, price) is not None:
            return None

        notional = qty * price
        margin = notional / self.cfg.leverage
        if margin > self.st["balance"]:
            return None
        return {**sizing, "qty": qty, "notional": notional, "margin": margin}

    # ── open / close ─────────────────────────────────────────────────
    def open_position(self, symbol: str, side: str, candle: pd.Series,
                      candle_n: int, now: datetime) -> None:
        price = float(candle["close"])
        atr = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0.0

        if self.nostop:
            # No stop order. `stop_price` carries the LIQUIDATION price —
            # the level at which this trade dies — so state, logs and the
            # trade log keep one consistent "where does this end" field.
            stop = liq_price(side, price, self.cfg)
        else:
            mult = 8.0 if side == "long" else 6.0
            stop = (price - mult * atr) if side == "long" else (price + mult * atr)
            if atr <= 0:
                stop = price * (0.95 if side == "long" else 1.05)

        sizing = self._size_or_skip(symbol, price, stop, side)
        if sizing is None:
            return

        qty = sizing["qty"]
        fee_in = price * qty * self.cfg.fee_rate
        filled = self.ex.place_entry(symbol, side, qty, stop)["filled_price"] or price

        self.st["balance"] -= fee_in
        self.st["total_fees"] += fee_in
        armed_for(self.st, symbol)["bull" if side == "long" else "bear"] = 0
        self.st["positions"][symbol] = {
            "side": side, "entry_price": filled, "stop_price": stop,
            "quantity": qty, "margin": sizing["margin"], "entry_fee": fee_in,
            "entry_time": now.isoformat(), "exit_model": self.exit_model,
            "funding_paid": 0.0,
        }
        self._entry_candle_n[symbol] = candle_n

    def close_position(self, symbol: str, candle: pd.Series, reason: str,
                       candle_n: int, now: datetime) -> None:
        pos = self.st["positions"].get(symbol)
        if pos is None:
            return

        side, entry, stop = pos["side"], pos["entry_price"], pos["stop_price"]
        qty, fee_in = pos["quantity"], pos["entry_fee"]
        margin, funding = pos.get("margin", 0.0), pos.get("funding_paid", 0.0)
        liquidated = reason == "liquidated"

        exit_price = stop if reason in ("stop", "liquidated") else float(candle["close"])
        fee_out = exit_price * qty * self.cfg.fee_rate

        if liquidated:
            # Isolated margin: the exchange takes the margin posted and
            # nothing more. Funding already left the balance during the
            # hold, so add it back here to keep the TOTAL loss equal to
            # margin + entry fee rather than double-charging it.
            raw_pnl = -(margin - funding)
            fee_out = 0.0        # the liquidation fee is inside the margin
        elif side == "long":
            raw_pnl = (exit_price - entry) * qty
        else:
            raw_pnl = (entry - exit_price) * qty

        pnl = raw_pnl - fee_in - fee_out - funding

        # fee_in already left the balance at entry, so only fee_out is new.
        # Adding the full `pnl` charged the entry fee twice and meant
        # balance == start + total_pnl + dca could never reconcile.
        self.st["balance"] += raw_pnl - fee_out
        self.st["total_fees"] += fee_out
        self.st["total_trades"] += 1
        self.st["total_pnl"] += pnl

        if pnl > 0:
            self.st["total_wins"] += 1
            self.st["consecutive_losses"] = 0
        else:
            self.cb_on_loss(now)

        # Exactly ONE ratchet call per trade, unconditionally. The old
        # guarded version reached CorpusManager zero times for a loss that
        # closed during an older CB pause, desyncing the ratchet from the
        # backtests.
        self.cm.on_trade_complete(pnl, self.st["balance"])

        if self.on_trade:
            self.on_trade({
                "datetime": now.isoformat(), "symbol": symbol, "side": side,
                "entry": entry, "exit": exit_price, "stop": stop, "qty": qty,
                "pnl": pnl, "fees": fee_in + fee_out, "balance": self.st["balance"],
                "reason": reason,
                "hold_candles": candle_n - self._entry_candle_n.get(symbol, candle_n),
                "config_id": f"{self.cfg.market_id}-{self.exit_model}-"
                             f"{int(self.cfg.leverage)}x",
            })

        self.st["positions"].pop(symbol, None)

    # ── the per-candle loop ──────────────────────────────────────────
    def process_candle(self, symbol: str, candle: pd.Series, candle_n: int,
                       now: datetime) -> None:
        """One closed candle for one symbol. Mirrors bot.py:1154 exactly."""
        # 1. DCA — once per market, not once per symbol.
        self.check_dca(now)

        # 2. Corpus snapshot BEFORE any exit on this bar can ratchet it.
        if self._corpus_this_candle is None:
            self._corpus_this_candle = self.cm.corpus

        # 3. Armed counters (per symbol).
        arm = armed_for(self.st, symbol)
        if bool(candle.get("bull_div", False)):
            arm["bull"] = self.cfg.div_memory
        elif arm["bull"] > 0:
            arm["bull"] -= 1
        if bool(candle.get("bear_div", False)):
            arm["bear"] = self.cfg.div_memory
        elif arm["bear"] > 0:
            arm["bear"] -= 1

        bull, bear = arm["bull"], arm["bear"]

        # 4. Funding.
        if self.nostop:
            self.charge_funding(symbol, candle)

        # 5. Exits.
        pos = self.st["positions"].get(symbol)
        if pos is not None:
            side, stop = pos["side"], pos["stop_price"]
            price = float(candle["close"])
            atr = float(candle["atr"]) if not math.isnan(candle["atr"]) else 0.0

            if self.nostop:
                # Liquidation checked INTRABAR against high/low. Every other
                # exit triggers on the close, but the exchange does not wait
                # for the close — checking it there would hide most
                # liquidations behind wicks and flatter the result.
                hit_liq = (float(candle["low"]) <= stop if side == "long"
                           else float(candle["high"]) >= stop)
                if hit_liq:
                    self.close_position(symbol, candle, "liquidated", candle_n, now)
                else:
                    opp = ((bear > 0 and bool(candle.get("macd_bear_cross")))
                           if side == "long" else
                           (bull > 0 and bool(candle.get("macd_bull_cross"))))
                    if opp and bool(candle.get("high_vol")):
                        self.close_position(symbol, candle, "signal", candle_n, now)
            else:
                if side == "long":
                    sig = bear > 0 and bool(candle.get("macd_bear_cross")) \
                        and bool(candle.get("high_vol"))
                    stp = atr > 0 and price <= stop
                else:
                    sig = bull > 0 and bool(candle.get("macd_bull_cross")) \
                        and bool(candle.get("high_vol"))
                    stp = atr > 0 and price >= stop
                if sig:
                    self.close_position(symbol, candle, "signal", candle_n, now)
                elif stp:
                    self.close_position(symbol, candle, "stop", candle_n, now)

        # 6. Entries — only if flat in this symbol, CB clear, and the
        #    universe-wide concurrency cap not already reached.
        if self.st["positions"].get(symbol) is not None:
            return
        paused, _ = self.cb_is_paused(now)
        if paused:
            return
        if len(self.st["positions"]) >= self.cfg.max_concurrent_positions:
            return

        if bull > 0 and bool(candle.get("macd_bull_cross")) and bool(candle.get("high_vol")):
            self.open_position(symbol, "long", candle, candle_n, now)
        elif bear > 0 and bool(candle.get("macd_bear_cross")) and bool(candle.get("high_vol")):
            self.open_position(symbol, "short", candle, candle_n, now)

    def end_of_bar(self) -> None:
        """Clear the per-bar corpus snapshot. Call after every symbol is done."""
        self._corpus_this_candle = None
