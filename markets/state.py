"""
markets/state.py — multi-position state, atomically written.

bot.py's bot_state.json has ONE `position` field (a dict or None), which is
exactly right for one symbol and cannot represent a 50-symbol Nifty universe.
Here `positions` is a dict keyed by symbol, and `armed` likewise — the
divergence memory counters are per-symbol too, which is easy to miss because
with a universe of one they look like globals.

Writes use the same temp + fsync + os.replace pattern bot.py adopted in
6f59223. fsync BEFORE the swap, so content survives host power loss and not
merely process death; PID-unique temp names so concurrent writers cannot
clobber each other.
"""
from __future__ import annotations

import json
import os
from typing import Any


def new_state(start_balance: float) -> dict:
    return {
        "balance": start_balance,
        "positions": {},          # symbol -> position dict
        "armed": {},              # symbol -> {"bull": int, "bear": int}
        "cb_pause_until": None,
        "consecutive_losses": 0,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "last_dca_month": None,
        "last_candle_ts": {},     # per symbol
        "last_updated_at": None,
    }


def armed_for(state: dict, symbol: str) -> dict:
    return state["armed"].setdefault(symbol, {"bull": 0, "bear": 0})


def atomic_write_json(path: str, payload: Any) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default
