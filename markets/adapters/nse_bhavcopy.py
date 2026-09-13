"""
╔══════════════════════════════════════════════════════════════════════╗
║  markets/adapters/nse_bhavcopy.py — NSE daily EOD, free and official  ║
╠══════════════════════════════════════════════════════════════════════╣
║  NSE publishes a "bhavcopy" every trading day: one file containing    ║
║  OHLCV for EVERY listed instrument. Free, no account, no API key,     ║
║  no rate-limited per-symbol quota. Verified 2026-09-13 back to 2000.  ║
║                                                                      ║
║  WHY THIS BEATS A PAID BROKER HISTORICAL API FOR THIS PROJECT         ║
║                                                                      ║
║  1. One request per DAY covers the whole market. A broker API would   ║
║     be 50 rate-limited per-symbol pulls for a Nifty-50 study, and     ║
║     500 for a wider universe. Here the universe is free.              ║
║  2. It carries ISIN. Ticker symbols get reused and renamed; ISINs do  ║
║     not. Keying history on TckrSymb silently splices two different    ║
║     companies together at a rename.                                   ║
║  3. IT LARGELY SOLVES SURVIVORSHIP BIAS. Because every listed stock   ║
║     appears every day, a point-in-time universe can be RECONSTRUCTED  ║
║     rather than approximated by projecting today's index membership   ║
║     backwards. That projection is the classic way to manufacture a    ║
║     fake edge in cross-sectional equity research: constituents are    ║
║     dropped for performing badly, so today's list has quietly         ║
║     excluded the losers. See build_universe() below.                  ║
║                                                                      ║
║  THE LIMITATION, STATED PLAINLY: daily EOD only. There is no          ║
║  intraday here, ever. If the research concludes it needs intraday     ║
║  bars, a paid feed becomes necessary and this module cannot help.     ║
║                                                                      ║
║  TWO FILE FORMATS, and the boundary is NOT hardcoded:                 ║
║    UDiFF  (current) — verified working 2024-01-01 -> today            ║
║    legacy (old)     — verified working 2000 -> ~2024-07, then 404s    ║
║  They overlap through H1 2024. Rather than trust a cutover date I     ║
║  inferred from five probes, each fetch tries the format likely for    ║
║  that date and falls back to the other on a 404.                      ║
║                                                                      ║
║  HOLIDAY vs MISSING: a 404 is NOT evidence of a gap. 2019-09-02       ║
║  404s because it was Ganesh Chaturthi. Every 404 is checked against   ║
║  the XBOM trading calendar; only a 404 on a real session is reported  ║
║  as a genuine hole worth investigating.                               ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import date, datetime

import pandas as pd
import requests

from ..calendar import SessionCalendar
from ..instruments import InstrumentSpec, nse_equity

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.environ.get("NSE_BHAV_CACHE",
                           os.path.join(_ROOT, "data_cache", "nse_bhavcopy"))

# NSE serves nothing to a bare client. A browser UA plus a Referer is the
# minimum that gets a 200; this is the same handshake a browser performs.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://www.nseindia.com/",
    "Accept": "text/html,application/xhtml+xml,*/*",
}

UDIFF_URL = ("https://nsearchives.nseindia.com/content/cm/"
             "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip")
LEGACY_URL = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
              "{y}/{mon}/cm{d:02d}{mon}{y}bhav.csv.zip")

# Observed boundary. Used only to choose which format to TRY FIRST — both
# are attempted, so being wrong here costs one extra request, not data.
UDIFF_FROM = date(2024, 1, 1)

# Politeness. These files are static once published and cached forever
# locally, so a slow first crawl is a one-off cost and there is no reason
# to hammer a free public archive.
REQUEST_DELAY_S = 0.35

# Normalised schema — the only shape the rest of the codebase sees.
COLUMNS = ["date", "symbol", "isin", "open", "high", "low", "close",
           "volume", "turnover", "trades"]

_UDIFF_MAP = {"TckrSymb": "symbol", "ISIN": "isin", "OpnPric": "open",
              "HghPric": "high", "LwPric": "low", "ClsPric": "close",
              "TtlTradgVol": "volume", "TtlTrfVal": "turnover",
              "TtlNbOfTxsExctd": "trades"}
_LEGACY_MAP = {"SYMBOL": "symbol", "ISIN": "isin", "OPEN": "open",
               "HIGH": "high", "LOW": "low", "CLOSE": "close",
               "TOTTRDQTY": "volume", "TOTTRDVAL": "turnover",
               "TOTALTRADES": "trades"}


class BhavcopyStore:
    """Downloads, normalises and caches NSE daily bhavcopy files."""

    def __init__(self, cache_dir: str = CACHE_DIR, session: requests.Session | None = None):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.calendar = SessionCalendar("XBOM")
        self._s = session or requests.Session()
        self._s.headers.update(HEADERS)

    # ── cache ────────────────────────────────────────────────────────
    def _cache_path(self, d: date) -> str:
        return os.path.join(self.cache_dir, f"{d.isoformat()}.csv.gz")

    def is_session(self, d: date) -> bool:
        """Is this a trading day? Outside calendar coverage, assume weekday."""
        ts = pd.Timestamp(d)
        try:
            return bool(self.calendar._xcal.is_session(ts))
        except Exception:
            return ts.weekday() < 5

    # ── fetch ────────────────────────────────────────────────────────
    def _urls(self, d: date) -> list[str]:
        udiff = UDIFF_URL.format(ymd=d.strftime("%Y%m%d"))
        legacy = LEGACY_URL.format(y=d.year, mon=d.strftime("%b").upper(), d=d.day)
        return [udiff, legacy] if d >= UDIFF_FROM else [legacy, udiff]

    def _download(self, d: date) -> pd.DataFrame | None:
        for url in self._urls(d):
            try:
                r = self._s.get(url, timeout=45)
            except requests.RequestException:
                continue
            time.sleep(REQUEST_DELAY_S)
            if r.status_code != 200 or not r.content[:2] == b"PK":
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                with z.open(name) as fh:
                    raw = pd.read_csv(fh)
            return self._normalise(raw, d)
        return None

    @staticmethod
    def _normalise(raw: pd.DataFrame, d: date) -> pd.DataFrame | None:
        """Map either format onto COLUMNS, keeping cash equities only."""
        raw.columns = [c.strip() for c in raw.columns]
        if "TckrSymb" in raw.columns:                       # UDiFF
            df = raw[raw["SctySrs"].astype(str).str.strip() == "EQ"].copy()
            if "FinInstrmTp" in df.columns:
                df = df[df["FinInstrmTp"].astype(str).str.strip() == "STK"]
            df = df.rename(columns=_UDIFF_MAP)
        elif "SYMBOL" in raw.columns:                       # legacy
            df = raw[raw["SERIES"].astype(str).str.strip() == "EQ"].copy()
            df = df.rename(columns=_LEGACY_MAP)
        else:
            return None
        if df.empty:
            return None
        df["date"] = pd.Timestamp(d)
        for c in COLUMNS:
            if c not in df.columns:
                df[c] = pd.NA
        df = df[COLUMNS]
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df["isin"] = df["isin"].astype(str).str.strip()
        for c in ("open", "high", "low", "close", "volume", "turnover", "trades"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"]).reset_index(drop=True)

    def day(self, d: date, *, use_cache: bool = True) -> pd.DataFrame | None:
        """One trading day, normalised. None if not a session or unavailable."""
        if not self.is_session(d):
            return None
        p = self._cache_path(d)
        if use_cache and os.path.exists(p):
            df = pd.read_csv(p, parse_dates=["date"])
            return df if not df.empty else None
        df = self._download(d)
        if df is not None and not df.empty:
            df.to_csv(p, index=False, compression="gzip")
        return df

    def load_range(self, start, end, *, progress_every: int = 250) -> pd.DataFrame:
        """Long-form panel for a date range. Reports only REAL gaps.

        A missing file on a non-session day is a holiday, not a hole. Only
        a session day that yields nothing is worth a human's attention —
        otherwise the report is thousands of false warnings and nobody
        reads the one that matters.
        """
        start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        days = [d.date() for d in pd.date_range(start, end, freq="D")]
        frames, missing, n = [], [], 0
        for d in days:
            if not self.is_session(d):
                continue
            n += 1
            df = self.day(d)
            if df is None or df.empty:
                missing.append(d)
            else:
                frames.append(df)
            if progress_every and n % progress_every == 0:
                print(f"    {n} sessions … {d}", flush=True)
        if missing:
            print(f"  ⚠ {len(missing)} SESSION days returned nothing "
                  f"(real gaps, not holidays): {[str(x) for x in missing[:5]]}"
                  f"{' …' if len(missing) > 5 else ''}")
        if not frames:
            return pd.DataFrame(columns=COLUMNS)
        return pd.concat(frames, ignore_index=True)


def build_universe(panel: pd.DataFrame, asof, top_n: int = 50,
                   lookback_days: int = 60, min_days: int = 40) -> list[str]:
    """POINT-IN-TIME universe: the top_n most liquid symbols as of `asof`.

    This is the survivorship-bias fix, and it is deliberately mechanical.

    Using today's Nifty 50 membership across history is biased: names are
    added after they have already done well and dropped after they have
    done badly, so the list encodes the answer. Ranking by TRAILING median
    turnover uses only information available on the day, so a symbol that
    was liquid in 2015 and later delisted is correctly included in 2015's
    universe and correctly absent from 2024's.

    Ranking on turnover rather than index membership is also reproducible
    without any index-constituent feed — which matters, because NSE's
    index-history endpoints returned 404/503 in testing and no broker API
    serves historical membership at all.

    `min_days` drops symbols that have not traded for most of the window,
    so a freshly-listed ticker with one enormous debut day cannot enter the
    universe on a single print.
    """
    asof = pd.Timestamp(asof)
    lo = asof - pd.Timedelta(days=lookback_days)
    w = panel[(panel["date"] > lo) & (panel["date"] <= asof)]
    if w.empty:
        return []
    g = w.groupby("symbol")["turnover"]
    liq = g.median()[g.count() >= min_days]
    return liq.sort_values(ascending=False).head(top_n).index.tolist()


class NSEBhavcopyAdapter:
    """DataAdapter over the bhavcopy panel. Daily bars, cash equities."""

    def __init__(self, store: BhavcopyStore | None = None,
                 panel: pd.DataFrame | None = None):
        self.store = store or BhavcopyStore()
        self.calendar = self.store.calendar
        self._panel = panel
        self._by_symbol: dict[str, pd.DataFrame] = {}

    def connect(self) -> None:
        if self._panel is None:
            raise RuntimeError(
                "Load a panel first: NSEBhavcopyAdapter(panel=store.load_range(...))"
            )

    def set_panel(self, panel: pd.DataFrame) -> None:
        self._panel = panel
        self._by_symbol.clear()

    def symbol_frame(self, symbol: str) -> pd.DataFrame:
        """OHLCV for one symbol, indexed by date — the engine's input shape."""
        if symbol not in self._by_symbol:
            d = self._panel[self._panel["symbol"] == symbol].copy()
            d = d.set_index("date").sort_index()
            self._by_symbol[symbol] = d[["open", "high", "low", "close", "volume"]]
        return self._by_symbol[symbol]

    def fetch_candles(self, symbol: str, limit: int) -> pd.DataFrame:
        return self.symbol_frame(symbol).tail(limit)

    def current_price(self, symbol: str) -> float:
        return float(self.symbol_frame(symbol)["close"].iloc[-1])

    def instrument(self, symbol: str) -> InstrumentSpec:
        return nse_equity(symbol)

    def get_position(self, symbol: str):
        raise NotImplementedError(
            "Bhavcopy is a historical data source with no venue attached. "
            "Returning None would assert 'the venue says flat', which this "
            "adapter is in no position to claim."
        )
