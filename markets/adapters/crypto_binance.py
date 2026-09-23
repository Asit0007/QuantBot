"""
markets/adapters/crypto_binance.py — Binance USD-M perpetuals.

Two modes behind one interface:

  OfflineCryptoAdapter — serves candles and funding from the CSV cache in
    data_cache/. No network. This is what the replay harness uses, and it
    is why the replay is deterministic and reproducible months from now:
    a live ccxt fetch would drift as Binance's history is amended.

  LiveCryptoAdapter — wraps ccxt.binanceusdm, mirroring bot.py's
    class Exchange (bot.py:426-607).

Both are DATA adapters only. Neither implements the TradingAdapter half, so
neither can place a real order — Phases 0-6 are research and paper, and a
missing method is a far better gate than a boolean one typo can flip.
`place_entry` here is a PAPER fill and says so.
"""
from __future__ import annotations

import os

import pandas as pd

from ..calendar import AlwaysOpenCalendar
from ..instruments import BTC_USDT_PERP, InstrumentSpec

_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data_cache",
)


def _slug(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


class OfflineCryptoAdapter:
    """Replay adapter — candles and funding from disk, nothing from the network."""

    def __init__(self, symbol: str = "BTC/USDT:USDT", timeframe: str = "15m",
                 exchange_id: str = "binanceusdm", cache_dir: str = _CACHE):
        self.calendar = AlwaysOpenCalendar()
        self.symbol = symbol
        self.timeframe = timeframe
        self.cache_dir = cache_dir
        self.exchange_id = exchange_id
        self._candles: pd.DataFrame | None = None
        self._funding: pd.Series | None = None

    # ── data ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        path = os.path.join(self.cache_dir,
                            f"{self.exchange_id}_{_slug(self.symbol)}_{self.timeframe}.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No cached candles at {path}. Populate data_cache/ first — the "
                f"replay is deliberately offline so its result cannot drift."
            )
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="mixed")
        self._candles = df.set_index("timestamp").sort_index()

        fpath = os.path.join(self.cache_dir,
                             f"{self.exchange_id}_{_slug(self.symbol)}_funding.csv")
        if os.path.exists(fpath):
            f = pd.read_csv(fpath)
            f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True, format="mixed")
            self._funding = f.set_index("timestamp")["rate"].sort_index()

    def all_candles(self) -> pd.DataFrame:
        if self._candles is None:
            self.connect()
        return self._candles

    def fetch_candles(self, symbol: str, limit: int) -> pd.DataFrame:
        return self.all_candles().tail(limit)

    def current_price(self, symbol: str) -> float:
        return float(self.all_candles()["close"].iloc[-1])

    def instrument(self, symbol: str) -> InstrumentSpec:
        return BTC_USDT_PERP

    def get_position(self, symbol: str):
        # Offline: the engine's own state is the only authority. Returning
        # None would assert "the venue says flat", which is a claim this
        # adapter is in no position to make.
        raise NotImplementedError("OfflineCryptoAdapter has no venue to reconcile against")

    def funding_in_window(self, symbol: str, start, end) -> list[float]:
        """Rates for every settlement in [start, end). Empty if none.

        WINDOW, not a point lookup, and this distinction cost a debugging
        cycle. An `asof` lookup returns the rate in force at a timestamp —
        which means that when a settlement is MISSING from the history it
        silently returns the previous one, and a position gets charged for
        a settlement that never happened. Matching on the window instead
        charges exactly the settlements that exist, which is what
        backtest_nostop.py:221-229 does.

        It also generalises: a 1d bar contains three 8h settlements, so a
        point lookup would drop two of them.
        """
        if self._funding is None or len(self._funding) == 0:
            return []
        seg = self._funding.loc[(self._funding.index >= start)
                                & (self._funding.index < end)]
        return [float(r) for r in seg.to_numpy()]

    # ── paper execution ──────────────────────────────────────────────
    def place_entry(self, symbol: str, side: str, qty: float,
                    stop: float | None = None) -> dict:
        """PAPER fill. Returns filled_price=None so the engine uses the close.

        Deliberately not the live ticker. bot.py's paper mode fills entries
        at current_price() (~6s after the candle closed) while exiting at
        the candle close — an asymmetry that is a known reason live paper
        drifts from the replay. The replay fills both at the close so the
        two engines are comparable.
        """
        return {"filled_price": None, "stop_order_id": None, "paper": True}


class LiveCryptoAdapter:
    """Thin wrapper over ccxt.binanceusdm. Mirrors bot.py's class Exchange."""

    def __init__(self, symbol: str = "BTC/USDT:USDT"):
        import ccxt
        self.calendar = AlwaysOpenCalendar()
        self.symbol = symbol
        self._ex = ccxt.binanceusdm({"enableRateLimit": True,
                                     "options": {"defaultType": "future"}})
        self._resolved = None
        self._limits = None

    def connect(self) -> None:
        from ..base import AdapterNetworkError
        try:
            self._ex.load_markets()
        except Exception as e:
            raise AdapterNetworkError(f"load_markets failed: {e}") from e
        for cand in (self.symbol, f"{self.symbol}:USDT"):
            if cand in self._ex.symbols:
                self._resolved = cand
                break
        if not self._resolved:
            raise RuntimeError(f"Symbol {self.symbol} not found on Binance futures")

    def fetch_candles(self, symbol: str, limit: int) -> pd.DataFrame:
        from ..base import AdapterNetworkError
        try:
            raw = self._ex.fetch_ohlcv(self._resolved or symbol, "15m", limit=limit)
        except Exception as e:
            raise AdapterNetworkError(str(e)) from e
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low",
                                        "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        # Drop the still-forming candle: a partial bar puts a moving number
        # into every indicator and the signal changes under the engine.
        return df.set_index("timestamp").iloc[:-1]

    def current_price(self, symbol: str) -> float:
        return float(self._ex.fetch_ticker(self._resolved or symbol)["last"])

    def instrument(self, symbol: str) -> InstrumentSpec:
        """Read the venue's CURRENT limits, never a cached constant.

        The lot step's dollar value tracks the underlying price, which is
        precisely how the $100 go-live blocker hid from ~30 backtests.
        """
        m = self._ex.market(self._resolved or symbol)
        lim = m.get("limits", {})
        return InstrumentSpec(
            symbol=self._resolved or symbol,
            lot_step=float(m.get("precision", {}).get("amount") or 0.001),
            min_qty=float((lim.get("amount") or {}).get("min") or 0.001),
            min_notional=float((lim.get("cost") or {}).get("min") or 0.0),
            quote_ccy="USDT",
        )

    def get_position(self, symbol: str):
        from ..base import AdapterNetworkError
        try:
            for p in self._ex.fetch_positions([self._resolved or symbol]):
                if float(p.get("contracts") or 0) != 0:
                    return p
            return None
        except Exception as e:
            # MUST raise. "I could not reach the exchange" and "there is no
            # position" are opposite facts; conflating them is how a phantom
            # position persists indefinitely.
            raise AdapterNetworkError(f"fetch_positions failed: {e}") from e

    def funding_rate(self, symbol: str, ts=None) -> float:
        return float(self._ex.fetch_funding_rate(
            self._resolved or symbol)["fundingRate"])

    def place_entry(self, symbol: str, side: str, qty: float,
                    stop: float | None = None) -> dict:
        raise NotImplementedError(
            "LiveCryptoAdapter is a DATA adapter. Order placement is "
            "deliberately absent for the research phases — implement "
            "TradingAdapter explicitly when going live."
        )
