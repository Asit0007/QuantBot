"""
╔══════════════════════════════════════════════════════════════════════╗
║  ohlcv_cache.py — on-disk CSV cache for historical OHLCV candles     ║
╠══════════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                             ║
║    Every backtest_*.py script re-downloaded the full history from    ║
║    Binance on every run — ~245 requests / ~244,000 candles for the   ║
║    6.9-year 15m BTC series, several minutes per run, and a different ║
║    dataset every day (the series keeps growing), so two runs of the  ║
║    same script were never exactly comparable.                        ║
║                                                                      ║
║    This module keeps one CSV per (exchange, symbol, timeframe) and    ║
║    only ever downloads the candles it does not already hold:         ║
║      • nothing cached      -> full download, then write the CSV      ║
║      • cache ends earlier  -> download only the tail (top-up)        ║
║      • cache starts later  -> download only the head (backfill)      ║
║      • cache already spans -> ZERO API requests                      ║
║                                                                      ║
║  USAGE (library — this is what the backtests call)                   ║
║      import ohlcv_cache                                              ║
║      df = ohlcv_cache.load_ohlcv(exchange, "BTC/USDT", "15m",        ║
║                                  "2019-09-01")                       ║
║    Returns exactly what the old fetch_ohlcv() returned: a float      ║
║    DataFrame indexed by UTC timestamp with open/high/low/close/      ║
║    volume columns, ascending, deduped.                               ║
║                                                                      ║
║  USAGE (CLI — warm or inspect the cache without running a backtest)  ║
║      python ohlcv_cache.py --warm    --symbol BTC/USDT --tf 15m      ║
║      python ohlcv_cache.py --info                                    ║
║      python ohlcv_cache.py --warm --tf 15m --force   # ignore cache  ║
║                                                                      ║
║  CONSTRAINTS / THINGS TO KNOW                                        ║
║    • ONLY CLOSED CANDLES ARE CACHED. The still-forming candle is     ║
║      dropped before writing, otherwise a partial bar would be        ║
║      frozen into the CSV forever and every later run would backtest  ║
║      against a fake OHLC. This is the single most important rule in  ║
║      this file.                                                      ║
║    • Top-up is tail/head only — it cannot heal a hole in the MIDDLE  ║
║      of a cached range (Binance occasionally omits candles). Run     ║
║      with force=True / --force to re-download from scratch if        ║
║      --info reports gaps you do not trust.                           ║
║    • The cache directory is gitignored. It is derived data: safe to  ║
║      delete at any time, it just costs one slow run to rebuild.      ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd

# ── Cache location ────────────────────────────────────────────────────
# Sits next to this file, not in DATA_DIR: this is research data, not bot
# runtime state, and must never end up on the trading VM's volume.
CACHE_DIR = os.environ.get(
    "OHLCV_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache"),
)

COLUMNS = ["open", "high", "low", "close", "volume"]

# Fallback timeframe table for the (unlikely) case where the exchange
# object cannot parse a timeframe string itself.
_TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
}


# ══════════════════════════════════════════════════════════════════════
#  Paths / helpers
# ══════════════════════════════════════════════════════════════════════

def _slug(text: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC-USDT-USDT'  (filesystem-safe, still readable)."""
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch in "-_.") else "-")
    return "".join(out).strip("-")


def cache_path(exchange, symbol: str, timeframe: str) -> str:
    """
    One CSV per (exchange id, symbol, timeframe).

    The exchange id is part of the name on purpose: binance SPOT and
    binanceusdm FUTURES quote the same symbol with different prices and
    volumes, and silently mixing them would corrupt every backtest that
    depends on volume spikes.
    """
    ex_id = getattr(exchange, "id", None) or "offline"
    return os.path.join(
        CACHE_DIR, f"{_slug(ex_id)}_{_slug(symbol)}_{_slug(timeframe)}.csv"
    )


def _tf_ms(exchange, timeframe: str) -> int:
    try:
        return int(exchange.parse_timeframe(timeframe)) * 1000
    except Exception:
        if timeframe not in _TF_SECONDS:
            raise ValueError(f"Unknown timeframe {timeframe!r}")
        return _TF_SECONDS[timeframe] * 1000


def _parse_since(exchange, since_str: str) -> int:
    """'2019-09-01' -> epoch ms (UTC midnight)."""
    if exchange is not None:
        try:
            return int(exchange.parse8601(f"{since_str}T00:00:00Z"))
        except Exception:
            pass
    dt = datetime.strptime(since_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _read_cache(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(path)
    except Exception as e:                                   # corrupt/truncated
        print(f"    ⚠️  cache unreadable ({e}) — refetching from scratch")
        return pd.DataFrame(columns=COLUMNS)
    if "timestamp" not in df.columns or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp")
    df = df[[c for c in COLUMNS if c in df.columns]].sort_index()
    return df.astype(float)


def _meta_path(path: str) -> str:
    return f"{path}.meta.json"


def _read_meta(path: str) -> dict:
    """
    Sidecar bookkeeping. Currently one key: earliest_probed_ms — the
    oldest `since` we have already asked the exchange for.

    Without it every run re-requests the week before the contract's
    listing date (the caller asks for 2019-09-01, BTC/USDT perp starts
    2019-09-08) and burns a 1000-candle request to learn nothing.
    """
    try:
        with open(_meta_path(path)) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_meta(path: str, meta: dict) -> None:
    tmp = f"{_meta_path(path)}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, _meta_path(path))


def _write_cache(path: str, df: pd.DataFrame) -> None:
    """
    Atomic write: a Ctrl-C halfway through a 244k-row dump must not leave a
    half-written CSV that the next run reads as gospel.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # PID-unique temp name: several backtests can legitimately run at once
    # (an attribution sweep does exactly that) and a shared "<path>.tmp"
    # makes them race — the first os.replace wins and the rest raise
    # FileNotFoundError on a temp file that no longer exists.
    tmp = f"{path}.tmp.{os.getpid()}"
    out = df.copy()
    out.index.name = "timestamp"
    out.to_csv(tmp)
    try:
        os.replace(tmp, path)
    except OSError:                       # another writer won — same data
        os.remove(tmp)


def _drop_forming_candle(df: pd.DataFrame, tf_ms: int) -> pd.DataFrame:
    """
    Keep only candles whose close time is already in the past.

    A candle stamped T is closed at T + tf. Anything newer is still being
    printed, and caching it would freeze a partial bar into the CSV.
    """
    if df.empty:
        return df
    now_ms   = int(time.time() * 1000)
    closed   = df.index.astype("int64") // 1_000_000 + tf_ms <= now_ms
    return df[closed]


# ══════════════════════════════════════════════════════════════════════
#  Network fetch (same pagination the backtests always used)
# ══════════════════════════════════════════════════════════════════════

def _download(exchange, symbol: str, timeframe: str,
              since_ms: int, until_ms: int | None,
              label: str, quiet: bool) -> pd.DataFrame:
    """
    Page forward from since_ms in 1000-candle chunks until the exchange
    stops returning full pages (or until_ms is passed).

    Returns whatever it managed to get: a partial download is still
    progress, because the next run resumes from the new cache tail.
    """
    rows, reqs, since = [], 0, since_ms
    while True:
        try:
            chunk = exchange.fetch_ohlcv(symbol, timeframe,
                                         since=since, limit=1000)
        except Exception as e:
            print(f"\n    ⚠️  {e}")
            time.sleep(2)
            break
        if not chunk:
            break
        rows.extend(chunk)
        since = chunk[-1][0] + 1
        reqs += 1
        if not quiet:
            stamp = datetime.fromtimestamp(
                chunk[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m")
            sys.stdout.write(f"\r    {label} {len(rows):>9,} candles "
                             f"({stamp}) [{reqs} req]  ")
            sys.stdout.flush()
        if until_ms is not None and since >= until_ms:
            break
        if len(chunk) < 1000:
            break
        time.sleep(0.05)

    if not quiet and reqs:
        print()

    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(rows, columns=["timestamp"] + COLUMNS)
    df = df.drop_duplicates(subset="timestamp")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    if until_ms is not None:
        df = df[df.index.astype("int64") // 1_000_000 < until_ms]
    return df.astype(float)


# ══════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════

def load_ohlcv(exchange, symbol: str, timeframe: str = "15m",
               since_str: str = "2019-09-01", *,
               force: bool = False, offline: bool = False,
               quiet: bool = False) -> pd.DataFrame:
    """
    Return the [since_str .. last closed candle] OHLCV series, downloading
    only what the CSV cache is missing.

    exchange  ccxt instance (may be None when offline=True)
    force     ignore the cache and re-download the whole range
    offline   never touch the network — serve whatever is cached, and
              raise if nothing is
    """
    path   = cache_path(exchange, symbol, timeframe)
    tf_ms  = _tf_ms(exchange, timeframe)
    want   = _parse_since(exchange, since_str)

    cached = pd.DataFrame(columns=COLUMNS) if force else _read_cache(path)
    meta   = {} if force else _read_meta(path)

    if offline or exchange is None:
        if cached.empty:
            raise RuntimeError(
                f"No cached candles at {path} and offline=True — run "
                f"`python ohlcv_cache.py --warm --symbol {symbol} "
                f"--tf {timeframe}` first."
            )
        if not quiet:
            _report(cached, path, tf_ms, "offline")
        return cached[cached.index.astype("int64") // 1_000_000 >= want]

    parts = [cached] if not cached.empty else []

    if cached.empty:
        if not quiet:
            print(f"    🌐 no cache — full download from {since_str}")
        parts.append(_download(exchange, symbol, timeframe, want, None,
                               "full  ", quiet))
    else:
        first_ms = int(cached.index[0].value // 1_000_000)
        last_ms  = int(cached.index[-1].value // 1_000_000)

        # Backfill: the cache starts later than the caller asked for.
        # One timeframe of tolerance, and never re-probe a `since` we have
        # already asked for — the exchange listing simply may not go back
        # as far as since_str, and that is not a cache miss.
        probed = meta.get("earliest_probed_ms")
        if want < first_ms - tf_ms and (probed is None or want < probed):
            meta["earliest_probed_ms"] = want
            if not quiet:
                print(f"    🌐 backfilling {since_str} → "
                      f"{cached.index[0]:%Y-%m-%d}")
            parts.insert(0, _download(exchange, symbol, timeframe,
                                      want, first_ms, "back  ", quiet))

        # Top-up: everything after the last cached candle.
        now_ms = int(time.time() * 1000)
        if last_ms + tf_ms <= now_ms - tf_ms:
            if not quiet:
                print(f"    🌐 topping up from {cached.index[-1]:%Y-%m-%d %H:%M}")
            parts.append(_download(exchange, symbol, timeframe,
                                   last_ms + 1, None, "top-up", quiet))

    df = pd.concat(parts) if parts else pd.DataFrame(columns=COLUMNS)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = _drop_forming_candle(df, tf_ms)

    # Only rewrite when we actually gained rows — keeps mtime meaningful.
    if len(df) != len(cached):
        _write_cache(path, df)
    if meta != _read_meta(path):
        _write_meta(path, meta)

    if not quiet:
        _report(df, path, tf_ms,
                "cache hit" if len(df) == len(cached) else "cache updated")

    return df[df.index.astype("int64") // 1_000_000 >= want].astype(float)


def load_funding(exchange, symbol: str,
                 since_str: str = "2019-09-01", *,
                 force: bool = False, quiet: bool = False) -> pd.Series:
    """
    Perpetual funding-rate history, cached the same way as candles.

    Returns a Series indexed by UTC settlement time, values = the rate
    applied at that settlement (0.0001 = 0.01%). Binance settles every 8h;
    a long pays `rate x notional` when the rate is positive and receives it
    when negative, a short the other way round.

    Why this exists: the stop-based backtests hold for a median of a few
    candles, so funding rounds to nothing and was ignored. A strategy with
    NO stop holds for weeks — 8h funding on a levered notional compounds
    into a first-order cost there, and ignoring it would flatter the
    no-stop result exactly where it matters.
    """
    path   = os.path.join(CACHE_DIR, f"{_slug(getattr(exchange,'id','offline'))}_"
                                     f"{_slug(symbol)}_funding.csv")
    cached = pd.Series(dtype=float) if force else _read_funding(path)
    want   = _parse_since(exchange, since_str)

    since = want if cached.empty else int(cached.index[-1].value // 1_000_000) + 1
    rows, reqs = [], 0
    while True:
        try:
            chunk = exchange.fetch_funding_rate_history(symbol, since=since, limit=1000)
        except Exception as e:
            print(f"\n    ⚠️  funding: {e}")
            break
        if not chunk:
            break
        rows.extend((c["timestamp"], float(c["fundingRate"])) for c in chunk)
        nxt = chunk[-1]["timestamp"] + 1
        if nxt <= since:
            break
        since, reqs = nxt, reqs + 1
        if not quiet:
            sys.stdout.write(f"\r    funding {len(rows):>7,} settlements [{reqs} req]  ")
            sys.stdout.flush()
        if len(chunk) < 1000:
            break
        time.sleep(0.05)
    if not quiet and reqs:
        print()

    if rows:
        fresh = pd.Series({pd.to_datetime(t, unit="ms", utc=True): r for t, r in rows})
        cached = fresh if cached.empty else pd.concat([cached, fresh])
        cached = cached[~cached.index.duplicated(keep="last")].sort_index()
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        cached.rename("rate").to_csv(tmp, index_label="timestamp")
        try:
            os.replace(tmp, path)
        except OSError:
            os.remove(tmp)

    if not quiet and not cached.empty:
        print(f"    📁 funding: {len(cached):,} settlements  "
              f"[{cached.index[0]:%Y-%m-%d} → {cached.index[-1]:%Y-%m-%d}]  "
              f"mean {cached.mean()*100:.4f}%/8h")
    return cached[cached.index.astype("int64") // 1_000_000 >= want]


def _read_funding(path: str) -> pd.Series:
    if not os.path.exists(path):
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(path)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")["rate"].astype(float).sort_index()
    except Exception:
        return pd.Series(dtype=float)


def gap_report(df: pd.DataFrame, tf_ms: int) -> list[tuple]:
    """Interior holes: (start, end, missing_candles). Empty list = clean."""
    if len(df) < 2:
        return []
    deltas = df.index.to_series().diff().dt.total_seconds().mul(1000)
    out = []
    for ts, d in deltas[deltas > tf_ms].items():
        missing = int(d // tf_ms) - 1
        out.append((ts - pd.Timedelta(milliseconds=d), ts, missing))
    return out


def _report(df: pd.DataFrame, path: str, tf_ms: int, verb: str) -> None:
    if df.empty:
        print(f"    📁 {verb}: EMPTY ({path})")
        return
    years = (df.index[-1] - df.index[0]).days / 365.25
    gaps  = gap_report(df, tf_ms)
    miss  = sum(g[2] for g in gaps)
    note  = f"  ⚠️ {len(gaps)} gaps / {miss:,} missing" if gaps else ""
    print(f"    📁 {verb}: {len(df):,} candles  "
          f"[{df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d %H:%M}] "
          f"{years:.1f}y  {os.path.basename(path)}{note}")


# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════

def _cli() -> None:
    p = argparse.ArgumentParser(description="Warm or inspect the OHLCV CSV cache")
    p.add_argument("--warm",   action="store_true", help="download / top up")
    p.add_argument("--info",   action="store_true", help="list cached files")
    p.add_argument("--force",  action="store_true", help="ignore cache, refetch")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--tf",     default="15m", help="15m, 4h, 1d, …")
    p.add_argument("--since",  default="2019-09-01")
    p.add_argument("--spot",   action="store_true",
                   help="use binance spot instead of binanceusdm futures")
    a = p.parse_args()

    if a.info or not a.warm:
        if not os.path.isdir(CACHE_DIR):
            print(f"No cache directory yet: {CACHE_DIR}")
            return
        files = sorted(f for f in os.listdir(CACHE_DIR) if f.endswith(".csv"))
        if not files:
            print(f"Cache directory empty: {CACHE_DIR}")
            return
        print(f"\n  📁 {CACHE_DIR}\n")
        total = 0
        for f in files:
            full = os.path.join(CACHE_DIR, f)
            mb   = os.path.getsize(full) / 1e6
            total += mb
            df   = _read_cache(full)
            if df.empty:
                print(f"    {f:<44} {mb:>6.1f} MB   (empty)")
                continue
            tf   = f.rsplit("_", 1)[-1].replace(".csv", "")
            gaps = gap_report(df, _TF_SECONDS.get(tf, 900) * 1000)
            print(f"    {f:<44} {mb:>6.1f} MB  {len(df):>8,} rows  "
                  f"{df.index[0]:%Y-%m-%d} → {df.index[-1]:%Y-%m-%d %H:%M}"
                  f"{'  ⚠️ ' + str(len(gaps)) + ' gaps' if gaps else ''}")
        print(f"\n    {'total':<44} {total:>6.1f} MB\n")
        return

    import ccxt
    exchange = (ccxt.binance({"enableRateLimit": True}) if a.spot else
                ccxt.binanceusdm({"enableRateLimit": True,
                                  "options": {"defaultType": "future"}}))
    print("📡 Connecting to Binance...")
    exchange.load_markets()
    sym = a.symbol if a.symbol in exchange.symbols else f"{a.symbol}:USDT"
    print(f"✅ {sym}  {a.tf}\n")
    load_ohlcv(exchange, sym, a.tf, a.since, force=a.force)


if __name__ == "__main__":
    _cli()
