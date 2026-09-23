"""
markets/adapters/us_french.py — US market returns, free, back to 1926.

The Ken French data library (Dartmouth) publishes daily Fama-French factors
built from CRSP. `Mkt-RF + RF` is the total return of the US equity market.
No API key, no rate limit, no bot wall — the three things that blocked every
other free US source: Yahoo returns 429, Stooq serves a SHA-256 proof-of-work
challenge, Tiingo 403, Alpaca 401.

WHAT THIS IS GOOD FOR, AND WHAT IT IS NOT.

  Good for: anything close-only over a very long history. 100 years is an
  extraordinary sample next to BTC's 6.9 — and sample size is this project's
  binding constraint, not ideas.

  NOT good for: anything needing intraday range. These are RETURNS, so a
  price series can be reconstructed by compounding but there is no genuine
  open/high/low. ATR stops, Parabolic SAR and intrabar liquidation checks all
  need high/low and CANNOT be computed here. Synthesising a high/low from a
  close would be inventing data — the same error as "repairing" a price move
  that was never a split. So this adapter reports close only, and any
  strategy needing range must wait for a real OHLCV feed (Alpaca).

  Also not individual stocks, and not SPX/NDX/DJI specifically — it is the
  whole US market, cap-weighted.
"""
from __future__ import annotations

import io
import os
import zipfile

import pandas as pd
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(_ROOT, "data_cache", "us_french")
BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
URL = BASE + "F-F_Research_Data_Factors_daily_CSV.zip"

# Regional daily factor files, same publisher and same columns. All verified
# reachable 2026-09-14. The international set starts 1990-07 (Bloomberg-based)
# against the US file's 1926-07 (CRSP-based) — different vendors, so treat the
# US series as a separate lineage rather than a longer version of the others.
REGIONS = {
    "US": "F-F_Research_Data_Factors_daily_CSV.zip",
    "EUROPE": "Europe_3_Factors_Daily_CSV.zip",
    "JAPAN": "Japan_3_Factors_Daily_CSV.zip",
    "ASIAPAC": "Asia_Pacific_ex_Japan_3_Factors_Daily_CSV.zip",
    "NORTHAM": "North_America_3_Factors_Daily_CSV.zip",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def load_region(region: str = "US", force: bool = False) -> pd.DataFrame:
    """Daily total-market return and compounded price index for one region.

    Columns: ret (decimal), rf (decimal), close (index, base 100).
    """
    if region not in REGIONS:
        raise KeyError(f"Unknown region {region!r}. Known: {sorted(REGIONS)}")
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"ff_{region.lower()}_daily.csv")
    if force or not os.path.exists(path):
        r = requests.get(BASE + REGIONS[region], headers=HEADERS, timeout=90)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0]).decode("latin-1")
        # Prose header, copyright footer, and the data block between them.
        # The international files PAD the date field ("19900702    ,") while
        # the US file does not, so strip before testing — a naive
        # ^\d{8}, match silently returns zero rows for every region but US.
        rows = []
        for ln in raw.splitlines():
            head = ln.split(",")[0].strip()
            if len(head) == 8 and head.isdigit():
                rows.append(",".join(x.strip() for x in ln.split(",")))
        df = pd.read_csv(io.StringIO("date,mkt_rf,smb,hml,rf\n" + "\n".join(rows)))
        df.to_csv(path, index=False)
    else:
        df = pd.read_csv(path)

    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
    # -99.99 is the publisher's missing-data marker. Dropping beats
    # forward-filling: a fabricated flat day would read as a real zero return.
    df = df[df["mkt_rf"] > -99]
    df = df.set_index("date").sort_index()
    # Values are PERCENT in the source file.
    df["ret"] = (df["mkt_rf"] + df["rf"]) / 100.0
    df["rf"] = df["rf"] / 100.0
    df["close"] = 100.0 * (1.0 + df["ret"]).cumprod()
    return df[["ret", "rf", "close"]]


def load_us_market(force: bool = False) -> pd.DataFrame:
    """Back-compat alias for load_region("US")."""
    return load_region("US", force=force)
